"""Creates measurements, hands tasks to nodes and turns their replies into stored results.

Rules the rest of the design depends on:

* Tasks are written to the database before anything is sent, so a crash never loses track of
  work that nodes may already be doing.
* Every change to a measurement happens in a transaction that holds that measurement's lock,
  appends to its event log, and only publishes to live streams after it commits.
* Task statuses only move forward. A late, duplicated or replayed frame can never undo a
  terminal state.
* "Offline" and "unreachable" are different facts. A node that disconnects ends its task as
  `node_disconnected`; it is never counted as packet loss at the target.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import (
    Measurement,
    MeasurementEvent,
    MeasurementTask,
    Node,
    Sample,
    new_id,
    new_measurement_id,
    utcnow,
)
from ..nodes import protocol as proto
from ..nodes.registry import NodeConnection, NodeRegistry
from ..runtime_settings import RuntimeSettings, load_settings
from ..security.ratelimit import SlidingWindowLimiter
from ..security.targets import (
    Denylist,
    Resolver,
    TargetError,
    address_is_public,
    parse_target,
    resolve_target,
    system_resolver,
)
from . import schemas
from .events import EventBus
from .schemas import TASK_ACTIVE, TASK_TERMINAL

log = logging.getLogger(__name__)

INTERVAL_MS = 1000
TIMEOUT_MS = 2000


class MeasurementError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class Owner:
    session_id: str | None
    user_id: int | None = None


@dataclass
class Tx:
    session: AsyncSession
    measurement: Measurement
    events: list[dict[str, Any]] = field(default_factory=list)
    after_commit: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    def emit(self, type_: str, data: dict[str, Any]) -> None:
        self.measurement.event_seq += 1
        seq = self.measurement.event_seq
        self.session.add(
            MeasurementEvent(measurement_id=self.measurement.id, seq=seq, type=type_, data=data)
        )
        self.events.append({"seq": seq, "type": type_, "data": data})


def _sample_out(sample: Sample) -> schemas.SampleOut:
    return schemas.SampleOut(
        seq=sample.seq,
        status=sample.status,
        rtt_ms=None if sample.rtt_us is None else round(sample.rtt_us / 1000, 3),
        error_code=sample.error_code,
    )


def task_state(task: MeasurementTask) -> schemas.TaskStateOut:
    return schemas.TaskStateOut(
        id=task.id,
        node=schemas.NodeInfo.model_validate(task.node_info),
        status=task.status,
        error_code=task.error_code,
        error_message=task.error_message,
        tested_ip=task.tested_ip,
        resolved_ips=list(task.resolved_ips or []),
        summary=schemas.Summary.model_validate(task.summary) if task.summary else None,
    )


class MeasurementEngine:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        registry: NodeRegistry,
        bus: EventBus,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._sessionmaker = sessionmaker
        self.registry = registry
        self.bus = bus
        self._resolver = resolver
        self._locks: dict[str, asyncio.Lock] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._create_limiter = SlidingWindowLimiter(limit=10, window_s=60)

    # ----------------------------------------------------------------------------------------
    # Plumbing
    # ----------------------------------------------------------------------------------------

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @asynccontextmanager
    async def _tx(self, measurement_id: str) -> AsyncIterator[Tx]:
        lock = self._locks.setdefault(measurement_id, asyncio.Lock())
        async with lock, self._sessionmaker() as session:
            measurement = (
                await session.execute(
                    select(Measurement).where(Measurement.id == measurement_id).with_for_update()
                )
            ).scalar_one()
            tx = Tx(session, measurement)
            yield tx
            await session.commit()
        for event in tx.events:
            self.bus.publish(measurement_id, event)
        for callback in tx.after_commit:
            self._spawn(callback())
        if tx.measurement.status != "running":
            self._locks.pop(measurement_id, None)

    async def _active_tasks(self, tx: Tx, node_id: str | None = None) -> list[MeasurementTask]:
        query = select(MeasurementTask).where(
            MeasurementTask.measurement_id == tx.measurement.id,
            MeasurementTask.status.in_(TASK_ACTIVE),
        )
        if node_id is not None:
            query = query.where(MeasurementTask.node_id == node_id)
        return list((await tx.session.execute(query)).scalars())

    async def _samples(self, session: AsyncSession, task_id: str) -> list[schemas.SampleOut]:
        rows = (
            await session.execute(
                select(Sample).where(Sample.task_id == task_id).order_by(Sample.seq)
            )
        ).scalars()
        return [_sample_out(s) for s in rows]

    # ----------------------------------------------------------------------------------------
    # Creating a measurement
    # ----------------------------------------------------------------------------------------

    async def create(
        self,
        request: schemas.CreateMeasurementRequest,
        owner: Owner,
        source_ip: str | None,
    ) -> str:
        async with self._sessionmaker() as session:
            settings = await load_settings(session)
            if not settings.measurements_enabled:
                raise MeasurementError(
                    "measurements_disabled",
                    "New measurements are temporarily disabled by the administrator.",
                    503,
                )
            await self._check_rate_and_concurrency(session, settings, owner, source_ip)

            try:
                parsed = parse_target(request.target)
                family = request.family
                resolved = await resolve_target(
                    parsed,
                    family=family,
                    denylist=Denylist.from_entries(settings.target_denylist),
                    resolver=self._resolver,
                )
            except TargetError as exc:
                raise MeasurementError(exc.code, exc.message, 422) from exc

            dns_mode = "fixed" if parsed.ip is not None else request.dns_mode
            count = request.count or settings.default_count
            if count > settings.max_count:
                raise MeasurementError(
                    "count_too_high", f"At most {settings.max_count} echo requests per node.", 422
                )

            nodes = await self._select_nodes(session, request, resolved.chosen_ip, dns_mode)
            probes = len(nodes) * count
            if probes > settings.max_probes_per_measurement:
                raise MeasurementError(
                    "probe_budget_exceeded",
                    f"{len(nodes)} nodes x {count} echo requests = {probes} probes, above the "
                    f"limit of {settings.max_probes_per_measurement} per measurement. Select "
                    "fewer nodes or fewer echo requests.",
                    422,
                )
            inflight = await self._inflight_probes(session)
            if inflight + probes > settings.max_global_inflight_probes:
                raise MeasurementError(
                    "busy", "The service is at capacity right now. Try again shortly.", 429
                )

            now = utcnow()
            deadline_s = max(settings.measurement_deadline_s, count * INTERVAL_MS // 1000 + 10)
            measurement = Measurement(
                id=new_measurement_id(),
                input_kind=parsed.input_kind,
                target_host=parsed.host,
                target_ip=str(resolved.chosen_ip)
                if dns_mode == "fixed" and resolved.chosen_ip
                else None,
                resolved_ips=resolved.resolved_ips,
                dns_mode=dns_mode,
                family=family,
                options={"count": count, "interval_ms": INTERVAL_MS, "timeout_ms": TIMEOUT_MS},
                owner_session_id=owner.session_id,
                owner_user_id=owner.user_id,
                source_ip=source_ip,
                created_at=now,
                deadline_at=now + timedelta(seconds=deadline_s),
            )
            session.add(measurement)
            tasks = [
                MeasurementTask(
                    id=new_id(),
                    measurement_id=measurement.id,
                    node_id=node.id,
                    attempt_id=new_id(),
                    status="pending",
                    resolved_ips=[],
                    node_info=schemas.NodeInfo(
                        id=node.id,
                        name=node.name,
                        country=node.country,
                        region=node.region,
                        city=node.city,
                        provider=node.provider,
                        asn=node.asn,
                        latitude=node.latitude,
                        longitude=node.longitude,
                        location_verified=node.location_verified,
                    ).model_dump(),
                    tested_ip=measurement.target_ip,
                )
                for node in nodes
            ]
            session.add_all(tasks)
            measurement.event_seq = 1
            first = {
                "tasks": [task_state(t).model_dump(mode="json") for t in tasks],
            }
            session.add(
                MeasurementEvent(
                    measurement_id=measurement.id, seq=1, type="measurement.created", data=first
                )
            )
            await session.commit()
            measurement_id = measurement.id

        log.info(
            "measurement created",
            extra={"measurement": measurement_id, "nodes": len(tasks), "kind": "ping"},
        )
        self.bus.publish(measurement_id, {"seq": 1, "type": "measurement.created", "data": first})
        self._spawn(self._dispatch(measurement_id))
        self._watchers[measurement_id] = asyncio.ensure_future(
            self._watch_deadline(measurement_id, deadline_s)
        )
        return measurement_id

    async def _check_rate_and_concurrency(
        self,
        session: AsyncSession,
        settings: RuntimeSettings,
        owner: Owner,
        source_ip: str | None,
    ) -> None:
        if source_ip:
            self._create_limiter.limit = settings.creations_per_minute_per_ip
            if not self._create_limiter.allow(source_ip):
                raise MeasurementError(
                    "rate_limited", "Too many measurements started. Wait a minute and retry.", 429
                )
            running_for_ip = await session.scalar(
                select(func.count())
                .select_from(Measurement)
                .where(Measurement.source_ip == source_ip, Measurement.status == "running")
            )
            if (running_for_ip or 0) >= settings.max_concurrent_per_ip:
                raise MeasurementError(
                    "too_many_running",
                    "Too many measurements are already running from your network.",
                    429,
                )
        if owner.session_id:
            running = await session.scalar(
                select(func.count())
                .select_from(Measurement)
                .where(
                    Measurement.owner_session_id == owner.session_id,
                    Measurement.status == "running",
                )
            )
            if (running or 0) >= settings.max_concurrent_per_session:
                raise MeasurementError(
                    "too_many_running",
                    f"You can run {settings.max_concurrent_per_session} measurements at a time. "
                    "Wait for one to finish or cancel it.",
                    429,
                )

    async def _inflight_probes(self, session: AsyncSession) -> int:
        rows = (
            await session.execute(
                select(MeasurementTask.measurement_id, func.count())
                .where(MeasurementTask.status.in_(TASK_ACTIVE))
                .group_by(MeasurementTask.measurement_id)
            )
        ).all()
        if not rows:
            return 0
        option_rows = (
            await session.execute(
                select(Measurement.id, Measurement.options).where(
                    Measurement.id.in_([r[0] for r in rows])
                )
            )
        ).all()
        options: dict[str, dict[str, Any]] = {row[0]: row[1] for row in option_rows}
        return sum(n * int(options[mid]["count"]) for mid, n in rows)

    async def _select_nodes(
        self,
        session: AsyncSession,
        request: schemas.CreateMeasurementRequest,
        chosen_ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None,
        dns_mode: str,
    ) -> list[Node]:
        """The nodes "all" means: approved, online, and able to run this probe, right now."""
        online = self.registry.online_ids()
        approved = (
            (await session.execute(select(Node).where(Node.status == "approved"))).scalars().all()
        )

        def capable(connection: NodeConnection) -> bool:
            caps = connection.capabilities
            if request.kind not in caps.kinds:
                return False
            if dns_mode == "fixed" and chosen_ip is not None:
                return caps.ipv4 if chosen_ip.version == 4 else caps.ipv6
            if request.family == "4":
                return caps.ipv4
            if request.family == "6":
                return caps.ipv6
            return caps.ipv4 or caps.ipv6

        eligible: list[Node] = []
        for node in approved:
            connection = self.registry.get(node.id)
            if node.id in online and connection is not None and capable(connection):
                eligible.append(node)
        eligible.sort(key=lambda n: (n.country or "~", n.city or "~", n.name))

        if request.node_ids is not None:
            wanted = set(request.node_ids)
            unavailable = sorted(wanted - {n.id for n in eligible})
            if unavailable:
                raise MeasurementError(
                    "node_unavailable",
                    "Some selected nodes are offline or cannot run this measurement: "
                    + ", ".join(unavailable),
                    409,
                )
            eligible = [n for n in eligible if n.id in wanted]
        if request.countries is not None:
            eligible = [n for n in eligible if (n.country or "") in set(request.countries)]
        if not eligible:
            raise MeasurementError(
                "no_eligible_nodes", "No online node can run this measurement right now.", 409
            )
        return eligible

    # ----------------------------------------------------------------------------------------
    # Dispatch, deadline, cancellation
    # ----------------------------------------------------------------------------------------

    async def _dispatch(self, measurement_id: str) -> None:
        async with self._sessionmaker() as session:
            measurement = await session.get(Measurement, measurement_id)
            if measurement is None:
                return
            tasks = (
                (
                    await session.execute(
                        select(MeasurementTask).where(
                            MeasurementTask.measurement_id == measurement_id,
                            MeasurementTask.status == "pending",
                        )
                    )
                )
                .scalars()
                .all()
            )
            options = proto.PingOptions.model_validate(measurement.options)
            if measurement.dns_mode == "fixed":
                target = proto.TaskTarget(ip=measurement.target_ip, family=measurement.family)
            else:
                target = proto.TaskTarget(
                    hostname=measurement.target_host, family=measurement.family
                )
            expires_at = measurement.deadline_at
            pending = [(t.id, t.node_id, t.attempt_id) for t in tasks]

        await asyncio.gather(
            *(
                self._send_task(
                    measurement_id,
                    proto.TaskFrame(
                        task_id=task_id,
                        attempt_id=attempt_id,
                        target=target,
                        options=options,
                        expires_at=expires_at,
                    ),
                    node_id,
                )
                for task_id, node_id, attempt_id in pending
            )
        )

    async def _send_task(self, measurement_id: str, frame: proto.TaskFrame, node_id: str) -> None:
        connection = self.registry.get(node_id)
        error: str | None = None
        if connection is None:
            error = "The node is no longer connected."
        else:
            try:
                await connection.send(frame)
            except Exception as exc:  # noqa: BLE001 - any send failure means the node is gone
                error = f"Could not send the task to the node ({type(exc).__name__})."
        async with self._tx(measurement_id) as tx:
            task = await tx.session.get(MeasurementTask, frame.task_id)
            if task is None or task.status != "pending":
                return  # the node already answered, or the task ended some other way
            if error is not None:
                await self._finish_task(tx, task, "node_disconnected", "node_disconnected", error)
                await self._maybe_finalize(tx)
                return
            task.status = "dispatched"
            task.dispatched_at = utcnow()
            tx.emit("task.updated", task_state(task).model_dump(mode="json"))

    async def _watch_deadline(self, measurement_id: str, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            return
        self._watchers.pop(measurement_id, None)
        async with self._tx(measurement_id) as tx:
            if tx.measurement.status != "running":
                return
            for task in await self._active_tasks(tx):
                self._queue_cancel(tx, task)
                await self._finish_task(
                    tx,
                    task,
                    "timed_out",
                    "deadline_exceeded",
                    proto.TASK_ERROR_CODES["deadline_exceeded"],
                )
            await self._maybe_finalize(tx)

    def _queue_cancel(self, tx: Tx, task: MeasurementTask) -> None:
        node_id, task_id = task.node_id, task.id

        async def send() -> None:
            connection = self.registry.get(node_id)
            if connection is not None:
                try:
                    await connection.send(proto.CancelFrame(task_id=task_id))
                except Exception:  # noqa: BLE001, S110 - best effort; the node also enforces expiry
                    pass

        tx.after_commit.append(send)

    async def cancel(self, measurement_id: str) -> None:
        async with self._tx(measurement_id) as tx:
            if tx.measurement.status != "running":
                return
            for task in await self._active_tasks(tx):
                self._queue_cancel(tx, task)
                await self._finish_task(
                    tx, task, "cancelled", "cancelled", proto.TASK_ERROR_CODES["cancelled"]
                )
            await self._finalize(tx, "cancelled")

    # ----------------------------------------------------------------------------------------
    # Task and measurement state transitions
    # ----------------------------------------------------------------------------------------

    async def _finish_task(
        self,
        tx: Tx,
        task: MeasurementTask,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        if task.status in TASK_TERMINAL:
            return False
        await tx.session.flush()
        samples = await self._samples(tx.session, task.id)
        task.status = status
        task.error_code = error_code
        task.error_message = error_message
        task.finished_at = utcnow()
        summary = schemas.summarize(samples)
        task.summary = summary.model_dump() if summary else None
        tx.emit("task.updated", task_state(task).model_dump(mode="json"))
        return True

    async def _maybe_finalize(self, tx: Tx) -> None:
        if tx.measurement.status != "running":
            return
        await tx.session.flush()
        if not await self._active_tasks(tx):
            await self._finalize(tx, "completed")

    async def _finalize(self, tx: Tx, status: str) -> None:
        if tx.measurement.status != "running":
            return
        await tx.session.flush()
        tasks = (
            (
                await tx.session.execute(
                    select(MeasurementTask).where(
                        MeasurementTask.measurement_id == tx.measurement.id
                    )
                )
            )
            .scalars()
            .all()
        )
        stats = schemas.compute_stats([task_state(t) for t in tasks])
        tx.measurement.status = status
        tx.measurement.finished_at = utcnow()
        tx.emit("measurement.finished", {"status": status, "stats": stats.model_dump(mode="json")})
        watcher = self._watchers.pop(tx.measurement.id, None)
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()
        log.info("measurement finished", extra={"measurement": tx.measurement.id, "status": status})

    # ----------------------------------------------------------------------------------------
    # Frames from nodes
    # ----------------------------------------------------------------------------------------

    async def _measurement_for_task(
        self, node_id: str, task_id: str, attempt_id: str
    ) -> str | None:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(
                    select(MeasurementTask.measurement_id).where(
                        MeasurementTask.id == task_id,
                        MeasurementTask.node_id == node_id,
                        MeasurementTask.attempt_id == attempt_id,
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            log.warning("frame for unknown task", extra={"node": node_id, "task": task_id})
        return row

    async def handle_frame(self, node_id: str, frame: Any) -> None:
        if isinstance(frame, proto.TaskAck):
            await self._on_ack(node_id, frame)
        elif isinstance(frame, proto.SampleFrame):
            await self._on_sample(node_id, frame)
        elif isinstance(frame, proto.TaskResult):
            await self._on_result(node_id, frame)

    async def _on_ack(self, node_id: str, frame: proto.TaskAck) -> None:
        measurement_id = await self._measurement_for_task(node_id, frame.task_id, frame.attempt_id)
        if measurement_id is None:
            return
        async with self._tx(measurement_id) as tx:
            task = await tx.session.get(MeasurementTask, frame.task_id)
            if task is not None and task.status in ("pending", "dispatched"):
                task.status = "running"
                tx.emit("task.updated", task_state(task).model_dump(mode="json"))

    async def _on_sample(self, node_id: str, frame: proto.SampleFrame) -> None:
        measurement_id = await self._measurement_for_task(node_id, frame.task_id, frame.attempt_id)
        if measurement_id is None:
            return
        async with self._tx(measurement_id) as tx:
            task = await tx.session.get(MeasurementTask, frame.task_id)
            if task is None or task.status not in TASK_ACTIVE:
                return
            if frame.seq >= int(tx.measurement.options["count"]):
                return
            exists = await tx.session.scalar(
                select(Sample.id).where(Sample.task_id == task.id, Sample.seq == frame.seq)
            )
            if exists is not None:
                return  # retransmission
            rtt_us = frame.rtt_us if frame.status == "ok" else None
            if frame.status == "ok" and rtt_us is None:
                return
            sample = Sample(
                task_id=task.id,
                seq=frame.seq,
                status=frame.status,
                rtt_us=rtt_us,
                error_code=frame.error_code,
            )
            tx.session.add(sample)
            if task.status in ("pending", "dispatched"):
                task.status = "running"
                tx.emit("task.updated", task_state(task).model_dump(mode="json"))
            data = _sample_out(sample).model_dump(mode="json")
            data["task_id"] = task.id
            tx.emit("sample", data)

    async def _on_result(self, node_id: str, frame: proto.TaskResult) -> None:
        measurement_id = await self._measurement_for_task(node_id, frame.task_id, frame.attempt_id)
        if measurement_id is None:
            return
        async with self._tx(measurement_id) as tx:
            task = await tx.session.get(MeasurementTask, frame.task_id)
            if task is None or task.status not in TASK_ACTIVE:
                return
            measurement = tx.measurement

            tested = _public_ip_or_none(frame.tested_ip)
            if (
                tested is not None
                and measurement.dns_mode == "fixed"
                and tested != measurement.target_ip
            ):
                await self._finish_task(
                    tx, task, "failed", "internal_error", "The node probed an unexpected address."
                )
            else:
                if tested is not None:
                    task.tested_ip = tested
                task.resolved_ips = [ip for ip in map(_public_ip_or_none, frame.resolved_ips) if ip]
                if frame.status == "completed":
                    await self._finish_task(tx, task, "completed")
                else:
                    code = (
                        frame.error_code
                        if frame.error_code in proto.TASK_ERROR_CODES
                        else "internal_error"
                    )
                    message = (
                        frame.error_message or proto.TASK_ERROR_CODES[code or "internal_error"]
                    )[:300]
                    await self._finish_task(tx, task, "failed", code, message)
            await self._maybe_finalize(tx)

    # ----------------------------------------------------------------------------------------
    # Node connection lifecycle and restart recovery
    # ----------------------------------------------------------------------------------------

    async def on_node_disconnected(self, node_id: str) -> None:
        async with self._sessionmaker() as session:
            measurement_ids = list(
                (
                    await session.execute(
                        select(MeasurementTask.measurement_id)
                        .where(
                            MeasurementTask.node_id == node_id,
                            MeasurementTask.status.in_(TASK_ACTIVE),
                        )
                        .distinct()
                    )
                ).scalars()
            )
        for measurement_id in measurement_ids:
            async with self._tx(measurement_id) as tx:
                for task in await self._active_tasks(tx, node_id):
                    await self._finish_task(
                        tx,
                        task,
                        "node_disconnected",
                        "node_disconnected",
                        proto.TASK_ERROR_CODES["node_disconnected"],
                    )
                await self._maybe_finalize(tx)

    def schedule_node_disconnected(self, node_id: str) -> None:
        """Run the disconnect clean-up as its own task.

        A connection handler can be cancelled (server shutdown, test harness). Awaiting database
        work from inside a cancelled task fails on its first await, which would leave the node's
        tasks looking alive until their deadline.
        """
        self._spawn(self.on_node_disconnected(node_id))

    async def recover_on_startup(self) -> int:
        """End measurements that were running when the previous process stopped."""
        async with self._sessionmaker() as session:
            ids = list(
                (
                    await session.execute(
                        select(Measurement.id).where(Measurement.status == "running")
                    )
                ).scalars()
            )
        for measurement_id in ids:
            async with self._tx(measurement_id) as tx:
                for task in await self._active_tasks(tx):
                    await self._finish_task(
                        tx,
                        task,
                        "interrupted",
                        "interrupted",
                        proto.TASK_ERROR_CODES["interrupted"],
                    )
                await self._finalize(tx, "interrupted")
        if ids:
            log.warning("interrupted measurements from previous run", extra={"count": len(ids)})
        return len(ids)

    async def shutdown(self) -> None:
        for watcher in list(self._watchers.values()):
            watcher.cancel()
        self._watchers.clear()
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)

    # ----------------------------------------------------------------------------------------
    # Reading
    # ----------------------------------------------------------------------------------------

    async def load(
        self, session: AsyncSession, measurement_id: str
    ) -> schemas.MeasurementOut | None:
        measurement = await session.get(Measurement, measurement_id)
        if measurement is None:
            return None
        tasks = (
            (
                await session.execute(
                    select(MeasurementTask)
                    .where(MeasurementTask.measurement_id == measurement_id)
                    .order_by(MeasurementTask.id)
                )
            )
            .scalars()
            .all()
        )
        sample_rows = (
            (
                await session.execute(
                    select(Sample)
                    .where(Sample.task_id.in_([t.id for t in tasks]))
                    .order_by(Sample.task_id, Sample.seq)
                )
            )
            .scalars()
            .all()
        )
        by_task: dict[str, list[schemas.SampleOut]] = {}
        for sample in sample_rows:
            by_task.setdefault(sample.task_id, []).append(_sample_out(sample))

        task_outs: list[schemas.TaskOut] = []
        for task in tasks:
            state = task_state(task)
            samples = by_task.get(task.id, [])
            if state.summary is None and task.status in TASK_ACTIVE:
                state.summary = schemas.summarize(samples)  # live running summary
            task_outs.append(schemas.TaskOut(**state.model_dump(), samples=samples))
        task_outs.sort(key=lambda t: (t.node.country or "~", t.node.city or "~", t.node.name, t.id))
        return schemas.MeasurementOut(
            id=measurement.id,
            kind=measurement.kind,
            status=measurement.status,
            target=schemas.TargetOut(
                host=measurement.target_host,
                ip=measurement.target_ip,
                resolved_ips=list(measurement.resolved_ips or []),
                input_kind=measurement.input_kind,
            ),
            dns_mode=measurement.dns_mode,
            family=measurement.family,
            options=schemas.PingOptionsOut.model_validate(measurement.options),
            shared=measurement.shared,
            created_at=measurement.created_at,
            deadline_at=measurement.deadline_at,
            finished_at=measurement.finished_at,
            last_event_seq=measurement.event_seq,
            stats=schemas.compute_stats(list(task_outs)),
            tasks=task_outs,
        )


def _public_ip_or_none(value: str | None) -> str | None:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    return str(ip) if address_is_public(ip) else None
