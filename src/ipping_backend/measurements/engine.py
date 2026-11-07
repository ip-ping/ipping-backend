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

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..cluster import CHANNEL_EVENTS, Cluster
from ..models import (
    Measurement,
    MeasurementEvent,
    MeasurementTask,
    Node,
    NodeConnection,
    Sample,
    new_id,
    new_measurement_id,
    utcnow,
)
from ..nodes import protocol as proto
from ..nodes.registry import NodeRegistry
from ..runtime_settings import RuntimeSettings, load_settings
from ..security.ratelimit import allow
from ..security.targets import (
    Denylist,
    ResolvedTarget,
    Resolver,
    TargetError,
    address_is_public,
    parse_target,
    resolve_target,
    system_resolver,
)
from . import schemas
from .plan import PlanError, plan_measurement, probe_cost, sample_limit
from .schemas import TASK_ACTIVE, TASK_TERMINAL

log = logging.getLogger(__name__)


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
    # A script's API key and what it may do per minute and per day (None: not a script).
    api_key_id: str | None = None
    per_minute: int | None = None
    per_day: int | None = None


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


def _sample_out(sample: Sample, kind: str = "ping") -> schemas.SampleOut:
    detail = proto.parse_detail(kind, sample.detail)
    return schemas.SampleOut(
        seq=sample.seq,
        status=sample.status,
        rtt_ms=None if sample.rtt_us is None else round(sample.rtt_us / 1000, 3),
        error_code=sample.error_code,
        http=detail if isinstance(detail, proto.HttpDetail) else None,
        hop=detail if isinstance(detail, proto.HopDetail) else None,
        dns=detail if isinstance(detail, proto.DnsDetail) else None,
    )


def task_state(task: MeasurementTask) -> schemas.TaskStateOut:
    return schemas.TaskStateOut(
        id=task.id,
        node=schemas.NodeInfo.model_validate(task.node_info),
        status=task.status,
        attempts=task.attempts or 1,
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
        cluster: Cluster,
        resolver: Resolver = system_resolver,
        *,
        dispatch_poll_s: float = 1.0,
        reaper_interval_s: float = 1.0,
        pending_grace_s: float = 5.0,
    ) -> None:
        self._sessionmaker = sessionmaker
        self.registry = registry
        self.cluster = cluster
        self._resolver = resolver
        self._dispatch_poll_s = dispatch_poll_s
        self._reaper_interval_s = reaper_interval_s
        self._pending_grace = timedelta(seconds=pending_grace_s)
        self._locks: dict[str, asyncio.Lock] = {}
        self._background: set[asyncio.Task[None]] = set()
        # Called (after commit) with the id of a finished measurement that a watch started.
        self.on_watch_finished: Callable[[str], Awaitable[None]] | None = None

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
            if tx.events and self.cluster.is_postgres:
                # Delivered to other processes only if (and when) this transaction commits.
                await session.execute(
                    text("SELECT pg_notify(:c, :p)"), {"c": CHANNEL_EVENTS, "p": measurement_id}
                )
            await session.commit()
        if tx.events:
            self.cluster.hub.wake(f"events:{measurement_id}")
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

    async def _samples(
        self, session: AsyncSession, task_id: str, kind: str
    ) -> list[schemas.SampleOut]:
        rows = (
            await session.execute(
                select(Sample).where(Sample.task_id == task_id).order_by(Sample.seq)
            )
        ).scalars()
        return [_sample_out(s, kind) for s in rows]

    # ----------------------------------------------------------------------------------------
    # Creating a measurement
    # ----------------------------------------------------------------------------------------

    async def create(
        self,
        request: schemas.CreateMeasurementRequest,
        owner: Owner,
        source_ip: str | None,
        watch_id: str | None = None,
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
                denylist = Denylist.from_entries(settings.target_denylist)
                if request.kind == "dns":
                    # A DNS check asks about the name; the backend never resolves it, so a name
                    # that does not exist (the interesting case) is a valid target.
                    if parsed.ip is not None:
                        raise TargetError(
                            "invalid_target", "A DNS check needs a name, not an IP address."
                        )
                    if denylist.blocks_host(parsed.host):
                        raise TargetError(
                            "blocked_by_policy",
                            f"{parsed.host} is on this deployment's blocklist.",
                        )
                    resolved = ResolvedTarget(parsed)
                else:
                    resolved = await resolve_target(
                        parsed, family=family, denylist=denylist, resolver=self._resolver
                    )
            except TargetError as exc:
                raise MeasurementError(exc.code, exc.message, 422) from exc

            if request.kind == "dns":
                dns_mode = "regional"  # every node asks for itself
            else:
                dns_mode = "fixed" if parsed.ip is not None else request.dns_mode
            try:
                plan = plan_measurement(request, parsed, settings)
            except PlanError as exc:
                raise MeasurementError(exc.code, exc.message, 422) from exc
            kind = request.kind
            each = probe_cost(kind, plan.options)

            nodes = await self._select_nodes(session, request, resolved.chosen_ip, dns_mode)
            probes = len(nodes) * each
            if probes > settings.max_probes_per_measurement:
                raise MeasurementError(
                    "probe_budget_exceeded",
                    f"{len(nodes)} nodes x {each} probes = {probes} probes, above the "
                    f"limit of {settings.max_probes_per_measurement} per measurement. Select "
                    "fewer nodes or fewer probes.",
                    422,
                )
            inflight = await self._inflight_probes(session)
            if inflight + probes > settings.max_global_inflight_probes:
                raise MeasurementError(
                    "busy", "The service is at capacity right now. Try again shortly.", 429
                )

            now = utcnow()
            measurement = Measurement(
                id=new_measurement_id(),
                kind=kind,
                input_kind=parsed.input_kind,
                target_host=parsed.host,
                target_ip=str(resolved.chosen_ip)
                if dns_mode == "fixed" and resolved.chosen_ip
                else None,
                resolved_ips=resolved.resolved_ips,
                dns_mode=dns_mode,
                family=family,
                options=plan.options,
                owner_session_id=owner.session_id,
                owner_user_id=owner.user_id,
                owner_api_key_id=owner.api_key_id,
                source_ip=source_ip,
                watch_id=watch_id,
                created_at=now,
                deadline_at=now + timedelta(seconds=plan.deadline_s),
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
            extra={"measurement": measurement_id, "nodes": len(tasks), "kind": kind},
        )
        # Whichever instance holds each node's connection sends that node's task; the deadline reaper
        # settles anything that is never answered.
        self.cluster.hub.wake(f"events:{measurement_id}")
        self.cluster.signal_tasks()
        return measurement_id

    async def _check_rate_and_concurrency(
        self,
        session: AsyncSession,
        settings: RuntimeSettings,
        owner: Owner,
        source_ip: str | None,
    ) -> None:
        if owner.api_key_id:
            await self._check_key_quota(session, settings, owner)
            return
        if source_ip:
            if not await allow(
                session, f"create:{source_ip}", settings.creations_per_minute_per_ip, 60
            ):
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

    async def _check_key_quota(
        self, session: AsyncSession, settings: RuntimeSettings, owner: Owner
    ) -> None:
        """A key has its own budget, counted from the measurements it made (so it cannot drift from
        reality), instead of the per-address and per-session limits that browsers get."""
        now = utcnow()

        async def made(since: timedelta) -> int:
            count = await session.scalar(
                select(func.count())
                .select_from(Measurement)
                .where(
                    Measurement.owner_api_key_id == owner.api_key_id,
                    Measurement.created_at > now - since,
                )
            )
            return count or 0

        if owner.per_minute is not None and await made(timedelta(minutes=1)) >= owner.per_minute:
            raise MeasurementError(
                "rate_limited", f"This key may start {owner.per_minute} measurements a minute.", 429
            )
        if owner.per_day is not None and await made(timedelta(days=1)) >= owner.per_day:
            raise MeasurementError(
                "rate_limited", f"This key may start {owner.per_day} measurements a day.", 429
            )
        running = await session.scalar(
            select(func.count())
            .select_from(Measurement)
            .where(
                Measurement.owner_api_key_id == owner.api_key_id, Measurement.status == "running"
            )
        )
        if (running or 0) >= settings.max_concurrent_per_session:
            raise MeasurementError(
                "too_many_running",
                f"This key can run {settings.max_concurrent_per_session} measurements at a time.",
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
                select(Measurement.id, Measurement.kind, Measurement.options).where(
                    Measurement.id.in_([r[0] for r in rows])
                )
            )
        ).all()
        costs: dict[str, int] = {row[0]: probe_cost(row[1], row[2]) for row in option_rows}
        return sum(n * costs[mid] for mid, n in rows)

    async def _select_nodes(
        self,
        session: AsyncSession,
        request: schemas.CreateMeasurementRequest,
        chosen_ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None,
        dns_mode: str,
    ) -> list[Node]:
        """The nodes "all" means: approved, online, and able to run this probe, right now."""
        online = await self.cluster.online_nodes(session)
        approved = (
            (await session.execute(select(Node).where(Node.status == "approved"))).scalars().all()
        )

        def capable(caps: proto.Capabilities) -> bool:
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
        online_approved = 0
        for node in approved:
            present = online.get(node.id)
            if present is not None:
                online_approved += 1
                if capable(present.capabilities):
                    eligible.append(node)
        eligible.sort(key=lambda n: (n.country or "~", n.city or "~", n.name))
        if not eligible and online_approved and request.kind != "ping":
            raise MeasurementError(
                "kind_not_supported",
                f"None of the {online_approved} online nodes can run a {request.kind} "
                "measurement yet. Nodes need to be updated to a newer version.",
                409,
            )

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

    async def _fail_undelivered(self, measurement_id: str, task_id: str, reason: str) -> None:
        async with self._tx(measurement_id) as tx:
            task = await tx.session.get(MeasurementTask, task_id)
            if task is None or task.status not in TASK_ACTIVE:
                return
            await self._finish_task(tx, task, "node_disconnected", "node_disconnected", reason)
            await self._maybe_finalize(tx)

    def _task_frame(self, task: MeasurementTask, measurement: Measurement) -> proto.TaskFrame:
        port = measurement.options.get("port") if measurement.kind in ("tcp", "http") else None
        if measurement.dns_mode == "fixed":
            # An http check connects to the pinned address but still presents the name it was given.
            named = measurement.kind == "http" and measurement.target_host != measurement.target_ip
            target = proto.TaskTarget(
                ip=measurement.target_ip,
                family=measurement.family,
                port=port,
                server_name=measurement.target_host if named else None,
            )
        else:
            target = proto.TaskTarget(
                hostname=measurement.target_host, family=measurement.family, port=port
            )
        return proto.TaskFrame(
            task_id=task.id,
            attempt_id=task.attempt_id,
            kind=measurement.kind,
            target=target,
            options=proto.TaskOptions.model_validate(measurement.options),
            expires_at=measurement.deadline_at,
        )

    async def _claim_and_send(self, measurement_id: str, task_id: str, node_id: str) -> None:
        """Take a pending task for a node connected *here*, record that, then send it.

        Claiming (pending -> dispatched, with this instance as owner) happens under the
        measurement's row lock before anything is sent, so two instances can never send the same
        task, and a crash between the two steps leaves a task whose owner is visibly dead.
        """
        async with self._tx(measurement_id) as tx:
            task = await tx.session.get(MeasurementTask, task_id)
            if task is None or task.status != "pending" or tx.measurement.status != "running":
                return
            connection = self.registry.get(node_id)
            if connection is None:
                return  # the node left in the meantime; the task stays pending for its next owner
            frame = self._task_frame(task, tx.measurement)
            task.status = "dispatched"
            task.dispatched_at = utcnow()
            task.owner_instance = self.cluster.instance_id
            tx.emit("task.updated", task_state(task).model_dump(mode="json"))

            async def deliver() -> None:
                try:
                    await connection.send(frame)
                except Exception as exc:  # noqa: BLE001 - any send failure means the node is gone
                    await self._fail_undelivered(
                        measurement_id,
                        frame.task_id,
                        f"Could not send the task to the node ({type(exc).__name__}).",
                    )

            tx.after_commit.append(deliver)

    async def dispatch_pending(self) -> int:
        """Send every pending task whose node is connected to this instance."""
        local = self.registry.online_ids()
        if not local:
            return 0
        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(
                        MeasurementTask.id, MeasurementTask.measurement_id, MeasurementTask.node_id
                    )
                    .join(Measurement, Measurement.id == MeasurementTask.measurement_id)
                    .where(
                        MeasurementTask.status == "pending",
                        MeasurementTask.node_id.in_(local),
                        Measurement.status == "running",
                    )
                    .order_by(Measurement.created_at)
                    .limit(500)
                )
            ).all()
        await asyncio.gather(*(self._claim_and_send(m, t, n) for t, m, n in rows))
        return len(rows)

    async def run_dispatcher(self) -> None:
        waiter = self.cluster.hub.waiter("tasks")
        try:
            while True:
                # Woken by new tasks or a node connecting; the timeout is the safety net for a
                # wake-up that never arrived.
                await waiter.wait(self._dispatch_poll_s)
                try:
                    await self.dispatch_pending()
                except Exception:
                    log.exception("dispatching failed")
        finally:
            self.cluster.hub.release(waiter)

    # ----------------------------------------------------------------------------------------
    # The reaper: deadlines, dead instances, abandoned tasks
    # ----------------------------------------------------------------------------------------

    async def _expire(self, measurement_id: str) -> None:
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

    async def _interrupt_orphans(self, measurement_id: str, alive: set[str]) -> None:
        """Tasks held by an instance that has stopped can never be answered through it.

        If the node had not produced anything yet, nothing is lost by sending the task again (a new
        attempt, so a late frame from the old one is ignored) and it waits for its node to reconnect,
        to whichever instance. Otherwise the partial results are kept and the task is interrupted.
        Only a backend failure is retried: a node that drops out stays `node_disconnected`, because
        hiding that would pass off a flaky node as a reliable one.
        """
        async with self._tx(measurement_id) as tx:
            if tx.measurement.status != "running":
                return
            retries = (await load_settings(tx.session)).task_retries
            interrupted = False
            for task in await self._active_tasks(tx):
                if task.owner_instance is None or task.owner_instance in alive:
                    continue
                if task.attempts <= retries and not await self._has_samples(tx, task):
                    self._requeue(tx, task)
                    continue
                interrupted |= await self._finish_task(
                    tx,
                    task,
                    "interrupted",
                    "interrupted",
                    proto.TASK_ERROR_CODES["interrupted"],
                )
            await tx.session.flush()
            if interrupted and not await self._active_tasks(tx):
                await self._finalize(tx, "interrupted")
            else:
                await self._maybe_finalize(tx)

    async def _has_samples(self, tx: Tx, task: MeasurementTask) -> bool:
        await tx.session.flush()
        found = await tx.session.scalar(select(Sample.id).where(Sample.task_id == task.id).limit(1))
        return found is not None

    def _requeue(self, tx: Tx, task: MeasurementTask) -> None:
        """Hand a task back to be sent again, as a new attempt."""
        task.attempts += 1
        task.attempt_id = new_id()
        task.status = "pending"
        task.owner_instance = None
        task.dispatched_at = None
        task.queued_at = utcnow()
        tx.emit("task.updated", task_state(task).model_dump(mode="json"))
        log.warning(
            "task sent again after its instance died",
            extra={"measurement": tx.measurement.id, "task": task.id, "attempt": task.attempts},
        )

        async def wake() -> None:
            self.cluster.signal_tasks()

        tx.after_commit.append(wake)

    async def _fail_stranded(self, measurement_id: str, online: set[str]) -> None:
        """Pending tasks whose node is gone and has not come back within the grace period."""
        async with self._tx(measurement_id) as tx:
            if tx.measurement.status != "running":
                return
            for task in await self._active_tasks(tx):
                waiting_since = task.queued_at or tx.measurement.created_at
                if utcnow() - waiting_since < self._pending_grace:
                    continue
                if task.status == "pending" and task.node_id not in online:
                    await self._finish_task(
                        tx,
                        task,
                        "node_disconnected",
                        "node_disconnected",
                        "The node went offline before it could receive the task.",
                    )
            await self._maybe_finalize(tx)

    async def reap(self) -> None:
        now = utcnow()
        async with self._sessionmaker() as session:
            alive = await self.cluster.alive_instance_ids(session)
            online = set((await self.cluster.online_nodes(session)).keys())
            expired = list(
                (
                    await session.execute(
                        select(Measurement.id)
                        .where(Measurement.status == "running", Measurement.deadline_at <= now)
                        .limit(100)
                    )
                ).scalars()
            )
            orphaned = list(
                (
                    await session.execute(
                        select(MeasurementTask.measurement_id)
                        .join(Measurement, Measurement.id == MeasurementTask.measurement_id)
                        .where(
                            Measurement.status == "running",
                            MeasurementTask.status.in_(("dispatched", "running")),
                            MeasurementTask.owner_instance.is_not(None),
                            MeasurementTask.owner_instance.not_in(alive),
                        )
                        .distinct()
                        .limit(100)
                    )
                ).scalars()
            )
            stranded = list(
                (
                    await session.execute(
                        select(MeasurementTask.measurement_id)
                        .join(Measurement, Measurement.id == MeasurementTask.measurement_id)
                        .where(
                            Measurement.status == "running",
                            Measurement.created_at < now - self._pending_grace,
                            MeasurementTask.status == "pending",
                            MeasurementTask.node_id.not_in(online),
                        )
                        .distinct()
                        .limit(100)
                    )
                ).scalars()
            )
            # Connections held by instances that are gone do not count as online any more.
            await session.execute(
                delete(NodeConnection).where(NodeConnection.instance_id.not_in(alive))
            )
            await session.commit()
        for measurement_id in expired:
            await self._expire(measurement_id)
        for measurement_id in orphaned:
            if measurement_id not in expired:
                await self._interrupt_orphans(measurement_id, alive)
        for measurement_id in stranded:
            if measurement_id not in expired:
                await self._fail_stranded(measurement_id, online)

    async def run_reaper(self) -> None:
        while True:
            try:
                await self.reap()
            except Exception:
                log.exception("reaping failed")
            await asyncio.sleep(self._reaper_interval_s)

    async def handle_command(self, command: dict[str, Any]) -> None:
        """Commands other instances broadcast: act on them if the node is connected here."""
        kind = command.get("type")
        node_id = str(command.get("node_id", ""))
        if kind == "close":
            if command.get("except") == self.cluster.instance_id:
                return
            connection = self.registry.get(node_id)
            if connection is not None:
                # Unregister before closing: until its handler notices, a connection that is being
                # closed would otherwise still look like a place to send new tasks to.
                self.registry.unregister(connection)
                await connection.close(
                    int(command.get("code", 4403)), str(command.get("reason", ""))
                )
                self.schedule_node_disconnected(node_id)
        elif kind == "cancel":
            connection = self.registry.get(node_id)
            if connection is not None:
                try:
                    await connection.send(proto.CancelFrame(task_id=str(command["task_id"])))
                except Exception:  # noqa: BLE001, S110 - best effort; the node also enforces expiry
                    pass

    def _queue_cancel(self, tx: Tx, task: MeasurementTask) -> None:
        node_id, task_id = task.node_id, task.id

        async def send() -> None:
            await self.cluster.command({"type": "cancel", "node_id": node_id, "task_id": task_id})

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
        samples = await self._samples(tx.session, task.id, tx.measurement.kind)
        task.status = status
        task.error_code = error_code
        task.error_message = error_message
        task.finished_at = utcnow()
        summary = schemas.summarize(samples, tx.measurement.kind)
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
        log.info("measurement finished", extra={"measurement": tx.measurement.id, "status": status})
        if tx.measurement.watch_id is not None and self.on_watch_finished is not None:
            hook, measurement_id = self.on_watch_finished, tx.measurement.id
            tx.after_commit.append(lambda: hook(measurement_id))

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
            if frame.seq >= sample_limit(tx.measurement.kind, tx.measurement.options):
                return
            exists = await tx.session.scalar(
                select(Sample.id).where(Sample.task_id == task.id, Sample.seq == frame.seq)
            )
            if exists is not None:
                return  # retransmission
            # A refused connection still took a measurable time to be refused, so an error sample
            # may carry one; a timeout never does.
            rtt_us = frame.rtt_us if frame.status in ("ok", "error") else None
            if frame.status == "ok" and rtt_us is None:
                return
            sample = Sample(
                task_id=task.id,
                seq=frame.seq,
                status=frame.status,
                rtt_us=rtt_us,
                error_code=frame.error_code,
                detail=_stored_detail(tx.measurement.kind, frame.detail),
            )
            tx.session.add(sample)
            if task.status in ("pending", "dispatched"):
                task.status = "running"
                tx.emit("task.updated", task_state(task).model_dump(mode="json"))
            data = _sample_out(sample, tx.measurement.kind).model_dump(mode="json")
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
                            MeasurementTask.owner_instance == self.cluster.instance_id,
                        )
                        .distinct()
                    )
                ).scalars()
            )
        for measurement_id in measurement_ids:
            async with self._tx(measurement_id) as tx:
                for task in await self._active_tasks(tx, node_id):
                    if task.owner_instance != self.cluster.instance_id:
                        continue  # not sent from here: its owner deals with it
                    await self._finish_task(
                        tx,
                        task,
                        "node_disconnected",
                        "node_disconnected",
                        proto.TASK_ERROR_CODES["node_disconnected"],
                    )
                await self._maybe_finalize(tx)

    async def _node_gone(self, node_id: str) -> None:
        await self.cluster.node_disconnected(node_id)
        await self.on_node_disconnected(node_id)

    def schedule_node_disconnected(self, node_id: str) -> None:
        """Run the disconnect clean-up as its own task.

        A connection handler can be cancelled (server shutdown, test harness). Awaiting database
        work from inside a cancelled task fails on its first await, which would leave the node
        looking online and its tasks looking alive until their deadline.
        """
        self._spawn(self._node_gone(node_id))

    async def shutdown(self) -> None:
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
            by_task.setdefault(sample.task_id, []).append(_sample_out(sample, measurement.kind))

        task_outs: list[schemas.TaskOut] = []
        for task in tasks:
            state = task_state(task)
            samples = by_task.get(task.id, [])
            if state.summary is None and task.status in TASK_ACTIVE:
                state.summary = schemas.summarize(samples, measurement.kind)  # live summary
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
            options=schemas.MeasurementOptions.model_validate(measurement.options),
            shared=measurement.shared,
            created_at=measurement.created_at,
            deadline_at=measurement.deadline_at,
            finished_at=measurement.finished_at,
            last_event_seq=measurement.event_seq,
            stats=schemas.compute_stats(list(task_outs)),
            tasks=task_outs,
        )


def _stored_detail(kind: str, raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """What a node sent along with a sample, checked and cleaned before it is stored.

    Anything that does not fit the model for the kind is dropped, and a traceroute hop that is not
    a public address is reduced to "a private hop": a result never discloses someone's LAN.
    """
    detail = proto.parse_detail(kind, raw)
    if detail is None:
        return None
    if isinstance(detail, proto.HopDetail):
        address = _public_ip_or_none(detail.ip)
        if detail.ip is not None and address is None:
            detail.private = True
        detail.ip = address
    if isinstance(detail, proto.DnsDetail):
        _clean_dns(detail)
    return detail.model_dump(mode="json", exclude_none=True)


def _clean_dns(detail: proto.DnsDetail) -> None:
    """The node already leaves private addresses out of an answer from its own resolver; this
    makes sure, whatever a node sends. A public resolver's answers are public data and stay."""
    if detail.resolver != "system":
        # Only a public address can be a resolver that was named; anything else is not shown.
        if _public_ip_or_none(detail.resolver) is None:
            detail.resolver = "unknown"
        return
    kept = []
    for record in detail.answers:
        if record.type in ("A", "AAAA") and _public_ip_or_none(record.data) is None:
            detail.redacted += 1
        else:
            kept.append(record)
    detail.answers = kept


def _public_ip_or_none(value: str | None) -> str | None:
    if not value:
        return None
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    return str(ip) if address_is_public(ip) else None
