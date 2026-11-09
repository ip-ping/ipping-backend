"""Running more than one backend process.

Everything that must be shared lives in the database; everything else is a hint.

* **Presence.** Every process is a row in `backend_instances` that it refreshes every few seconds;
  every node connection is a row in `node_connections` naming the process that holds the socket.
  A node is online when its row exists *and* its process is alive. A process that stops
  heartbeating is dead to everyone else after `instance_dead_after_s`, which is how a crash is
  noticed (a clean shutdown removes the row at once).
  A process that was paused (a suspended machine, a long stall, a database that was away for a
  while) is not dead, and must never decide that it is: the reaper always counts its own process as
  alive, and every heartbeat puts back the rows of connections the process really holds if someone
  removed them meanwhile, so a node never stays "offline" while its socket is open.
* **Wake-ups, not messages.** PostgreSQL `LISTEN/NOTIFY` tells other processes that "something
  changed, go look" (new tasks, new events, a command). Nothing depends on a notification arriving:
  every consumer also polls on a timer, and the work itself (tasks, events) is read from tables. A
  lost notification costs latency, never a result.
* **Commands** are the one thing that is only a notification: "close this node's socket", "cancel
  this task". They are best effort by design, because the node also enforces expiry, and the
  deadline reaper settles anything that was missed.

Without PostgreSQL (SQLite for development) there is one process, notifications become direct local
calls, and the same code paths run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from . import __version__
from .models import BackendInstance, NodeConnection, utcnow
from .nodes.protocol import Capabilities

log = logging.getLogger(__name__)

CHANNEL_TASKS = "ipping_tasks"
CHANNEL_EVENTS = "ipping_events"
CHANNEL_COMMANDS = "ipping_cmd"


@dataclass(eq=False)
class Waiter:
    topic: str
    event: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait(self, timeout: float) -> bool:
        """True if woken, False if the timeout passed (the caller polls either way)."""
        try:
            await asyncio.wait_for(self.event.wait(), timeout)
        except TimeoutError:
            return False
        finally:
            self.event.clear()
        return True


class Hub:
    """In-process wake-ups: local code and the PostgreSQL bridge both call `wake`."""

    def __init__(self) -> None:
        self._waiters: dict[str, set[Waiter]] = defaultdict(set)

    def waiter(self, topic: str) -> Waiter:
        waiter = Waiter(topic)
        self._waiters[topic].add(waiter)
        return waiter

    def release(self, waiter: Waiter) -> None:
        waiters = self._waiters.get(waiter.topic)
        if waiters is not None:
            waiters.discard(waiter)
            if not waiters:
                del self._waiters[waiter.topic]

    def wake(self, topic: str) -> None:
        for waiter in list(self._waiters.get(topic, ())):
            waiter.event.set()

    def wake_all(self) -> None:
        for topic in list(self._waiters):
            self.wake(topic)

    def count(self, prefix: str) -> int:
        return sum(len(w) for t, w in self._waiters.items() if t.startswith(prefix))


@dataclass(frozen=True, slots=True)
class OnlineNode:
    node_id: str
    instance_id: str
    capabilities: Capabilities
    protocol_version: int


CommandHandler = Callable[[dict[str, Any]], Awaitable[None]]


class Cluster:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
        database_url: str,
        *,
        heartbeat_s: float = 3.0,
        dead_after_s: float = 10.0,
        instance_id: str | None = None,
    ) -> None:
        self.instance_id = instance_id or uuid.uuid4().hex
        self.hub = Hub()
        self.heartbeat_s = heartbeat_s
        self.dead_after = timedelta(seconds=dead_after_s)
        self._sessionmaker = sessionmaker
        self._engine = engine
        self._database_url = database_url
        self.is_postgres = engine.dialect.name == "postgresql"
        self._command_handler: CommandHandler | None = None
        self._live_connections: Callable[[], list[tuple[str, Capabilities, int]]] | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._background: set[asyncio.Task[None]] = set()

    # ----------------------------------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------------------------------

    def on_command(self, handler: CommandHandler) -> None:
        self._command_handler = handler

    def on_live_connections(
        self, provider: Callable[[], list[tuple[str, Capabilities, int]]]
    ) -> None:
        """Where to ask which node sockets this process really holds: (node id, capabilities, protocol)."""
        self._live_connections = provider

    async def start(self) -> None:
        await self._heartbeat_once()
        self._tasks.append(asyncio.create_task(self._heartbeat_loop(), name="cluster-heartbeat"))
        if self.is_postgres:
            self._tasks.append(asyncio.create_task(self._bridge_loop(), name="cluster-bridge"))
        log.info(
            "backend instance started", extra={"instance": self.instance_id, "pid": os.getpid()}
        )

    async def stop(self) -> None:
        """Leave cleanly: other instances see this one gone immediately, not after a timeout."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, *self._background, return_exceptions=True)
        self._tasks.clear()
        try:
            async with self._sessionmaker() as db:
                await db.execute(
                    delete(NodeConnection).where(NodeConnection.instance_id == self.instance_id)
                )
                await db.execute(
                    delete(BackendInstance).where(BackendInstance.id == self.instance_id)
                )
                await db.commit()
        except Exception:  # noqa: BLE001 - shutting down; the heartbeat timeout covers it
            log.warning("could not remove this instance from the database on shutdown")

    async def _heartbeat_once(self) -> None:
        async with self._sessionmaker() as db:
            await db.merge(
                BackendInstance(id=self.instance_id, version=__version__, heartbeat_at=utcnow())
            )
            await db.commit()

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                await self._heartbeat_once()
                await self.restore_connections()
            except Exception:
                log.exception("heartbeat failed")

    async def restore_connections(self) -> int:
        """Put back the rows of connections this process holds but the database has lost.

        Another process (or this one, after a stall) may have taken this instance for dead and removed
        its rows while the sockets stayed open. A row is only added where there is none for the node:
        a node that has since reconnected to another instance is that instance's, not ours.
        """
        if self._live_connections is None:
            return 0
        live = self._live_connections()
        if not live:
            return 0
        restored = 0
        async with self._sessionmaker() as db:
            have = set(
                (
                    await db.execute(
                        select(NodeConnection.node_id).where(
                            NodeConnection.node_id.in_([node_id for node_id, _, _ in live])
                        )
                    )
                ).scalars()
            )
            for node_id, capabilities, protocol_version in live:
                if node_id in have:
                    continue
                db.add(
                    NodeConnection(
                        node_id=node_id,
                        instance_id=self.instance_id,
                        connected_at=utcnow(),
                        protocol_version=protocol_version,
                        capabilities=capabilities.model_dump(),
                    )
                )
                restored += 1
            try:
                await db.commit()
            except IntegrityError:  # another instance claimed one of them first: theirs wins
                await db.rollback()
                return 0
        if restored:
            log.warning(
                "restored %d node connection(s) that had been removed from the database",
                restored,
            )
            self.signal_tasks()
        return restored

    # ----------------------------------------------------------------------------------------
    # Who is alive, who is online
    # ----------------------------------------------------------------------------------------

    def _cutoff(self) -> Any:
        return utcnow() - self.dead_after

    async def alive_instance_ids(self, db: AsyncSession) -> set[str]:
        rows = await db.execute(
            select(BackendInstance.id).where(BackendInstance.heartbeat_at > self._cutoff())
        )
        return set(rows.scalars())

    async def online_nodes(self, db: AsyncSession) -> dict[str, OnlineNode]:
        rows = await db.execute(
            select(NodeConnection)
            .join(BackendInstance, BackendInstance.id == NodeConnection.instance_id)
            .where(BackendInstance.heartbeat_at > self._cutoff())
        )
        known = Capabilities.model_fields
        return {
            c.node_id: OnlineNode(
                c.node_id,
                c.instance_id,
                Capabilities.model_validate(
                    {k: v for k, v in (c.capabilities or {}).items() if k in known}
                ),
                c.protocol_version,
            )
            for c in rows.scalars()
        }

    async def node_connected(
        self, node_id: str, capabilities: Capabilities, protocol_version: int
    ) -> None:
        for attempt in (1, 2):
            try:
                async with self._sessionmaker() as db:
                    await db.merge(
                        NodeConnection(
                            node_id=node_id,
                            instance_id=self.instance_id,
                            connected_at=utcnow(),
                            protocol_version=protocol_version,
                            capabilities=capabilities.model_dump(),
                        )
                    )
                    await db.commit()
                break
            except (
                IntegrityError
            ):  # two instances inserted the same node at once; the second merge updates
                if attempt == 2:
                    raise
        # A node that reconnected here may still look connected to another instance.
        await self.command(
            {"type": "close", "node_id": node_id, "code": 4409, "except": self.instance_id}
        )
        self.signal_tasks()

    async def node_disconnected(self, node_id: str) -> None:
        """Forget the connection, but only if it is still this instance's (a reconnect elsewhere wins)."""
        async with self._sessionmaker() as db:
            await db.execute(
                delete(NodeConnection).where(
                    NodeConnection.node_id == node_id,
                    NodeConnection.instance_id == self.instance_id,
                )
            )
            await db.commit()

    # ----------------------------------------------------------------------------------------
    # Wake-ups and commands
    # ----------------------------------------------------------------------------------------

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _notify(self, channel: str, payload: str) -> None:
        if not self.is_postgres:
            return
        try:
            async with self._engine.begin() as conn:
                await conn.execute(text("SELECT pg_notify(:c, :p)"), {"c": channel, "p": payload})
        except Exception:
            log.warning("could not send a notification on %s", channel, exc_info=True)

    def signal_tasks(self) -> None:
        """New work may be waiting for a node: wake this instance's dispatcher and everyone else's."""
        self.hub.wake("tasks")
        if self.is_postgres:
            self._spawn(self._notify(CHANNEL_TASKS, ""))

    async def command(self, command: dict[str, Any]) -> None:
        """Run `command` here and tell the other instances. Best effort."""
        command = {**command, "origin": self.instance_id}
        await self._handle_command(command)
        await self._notify(CHANNEL_COMMANDS, json.dumps(command))

    async def _handle_command(self, command: dict[str, Any]) -> None:
        if self._command_handler is not None:
            try:
                await self._command_handler(command)
            except Exception:
                log.exception("command failed", extra={"command": command.get("type")})

    def _on_notification(self, channel: str, payload: str) -> None:
        if channel == CHANNEL_TASKS:
            self.hub.wake("tasks")
        elif channel == CHANNEL_EVENTS:
            self.hub.wake(f"events:{payload}")
        elif channel == CHANNEL_COMMANDS:
            try:
                command = json.loads(payload)
            except ValueError:
                return
            if command.get("origin") != self.instance_id:
                self._spawn(self._handle_command(command))

    # ----------------------------------------------------------------------------------------
    # The PostgreSQL LISTEN connection
    # ----------------------------------------------------------------------------------------

    async def _bridge_loop(self) -> None:
        import asyncpg  # only needed (and installed for use) with PostgreSQL

        dsn = self._database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        delay = 1.0
        while True:
            connection = None
            try:
                connection = await asyncpg.connect(dsn)

                def callback(_conn: Any, _pid: int, channel: str, payload: str) -> None:
                    self._on_notification(channel, payload)

                for channel in (CHANNEL_TASKS, CHANNEL_EVENTS, CHANNEL_COMMANDS):
                    await connection.add_listener(channel, callback)
                delay = 1.0
                closed = asyncio.Event()
                connection.add_termination_listener(lambda _c, event=closed: event.set())
                # Anything announced while we were not listening is found by polling: wake everyone.
                self.hub.wake_all()
                await closed.wait()
                log.warning("the notification connection closed; reconnecting")
            except asyncio.CancelledError:
                if connection is not None and not connection.is_closed():
                    await connection.close()
                raise
            except Exception:
                log.warning(
                    "notification connection failed; retrying in %.0fs", delay, exc_info=True
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)
