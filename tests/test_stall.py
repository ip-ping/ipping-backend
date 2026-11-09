"""A backend process that was paused (a suspended machine, a long stall, a database that was away) is
not dead. It must not take itself for dead and throw away the connections it holds: the nodes'
sockets are open, and a node whose row is gone shows as offline and gets no tasks until it
reconnects, which it has no reason to do."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select, update

from ipping_backend.models import BackendInstance, NodeConnection, utcnow

from .conftest import Api
from .test_api import connect


def _run(api: Api, coroutine_function: Any) -> Any:
    return api.client.portal.call(coroutine_function)


def _rows(api: Api) -> list[tuple[str, str]]:
    async def read() -> list[tuple[str, str]]:
        async with api.client.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
            found = (await db.execute(select(NodeConnection))).scalars()
            return [(row.node_id, row.instance_id) for row in found]

    return _run(api, read)  # type: ignore[no-any-return]


def _online(api: Api) -> list[bool]:
    return [n["online"] for n in api.get("/api/v1/nodes").json()]


def _one_node(api: Api) -> tuple[Any, str]:
    api.become_admin()
    node_id, credential = api.enroll_node()
    ws, _ = connect(api, credential)
    return ws, node_id


def test_a_process_that_was_paused_does_not_reap_its_own_connections(api: Api) -> None:
    ws, node_id = _one_node(api)
    try:
        assert _online(api) == [True] and len(_rows(api)) == 1

        async def pause_then_reap() -> None:
            app = api.client.app  # type: ignore[attr-defined]
            async with app.state.sessionmaker() as db:
                # the last heartbeat is an hour old, as after a suspended machine
                await db.execute(
                    update(BackendInstance).values(heartbeat_at=utcnow() - timedelta(hours=1))
                )
                await db.commit()
            await app.state.engine.reap()

        _run(api, pause_then_reap)
        rows = _rows(api)
        assert [r[0] for r in rows] == [node_id], f"the process reaped its own connection: {rows}"
    finally:
        ws.__exit__(None, None, None)


def test_connections_the_database_lost_are_put_back_while_the_socket_is_open(api: Api) -> None:
    ws, node_id = _one_node(api)
    try:

        async def lose_rows() -> None:
            async with api.client.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
                await db.execute(delete(NodeConnection))
                await db.commit()

        _run(api, lose_rows)
        assert _rows(api) == [] and _online(api) == [False]

        restored = _run(api, api.client.app.state.cluster.restore_connections)  # type: ignore[attr-defined]
        assert restored == 1
        assert [r[0] for r in _rows(api)] == [node_id] and _online(api) == [True]
        # and again: nothing is missing, nothing is added
        assert _run(api, api.client.app.state.cluster.restore_connections) == 0  # type: ignore[attr-defined]
    finally:
        ws.__exit__(None, None, None)


def test_a_node_that_reconnected_to_another_instance_is_not_taken_back(api: Api) -> None:
    ws, node_id = _one_node(api)
    try:

        async def move_to_another_instance() -> None:
            async with api.client.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
                await db.execute(delete(NodeConnection))
                db.add(BackendInstance(id="other", version="x", heartbeat_at=utcnow()))
                await db.flush()
                db.add(
                    NodeConnection(
                        node_id=node_id, instance_id="other", connected_at=utcnow(),
                        protocol_version=1, capabilities={"kinds": ["ping"]},
                    )
                )  # fmt: skip
                await db.commit()

        _run(api, move_to_another_instance)
        assert _run(api, api.client.app.state.cluster.restore_connections) == 0  # type: ignore[attr-defined]
        assert _rows(api) == [(node_id, "other")]
    finally:
        ws.__exit__(None, None, None)


def test_a_node_that_has_disconnected_is_not_put_back(api: Api) -> None:
    ws, _ = _one_node(api)
    ws.__exit__(None, None, None)

    async def settle_and_lose_rows() -> int:
        import asyncio

        app = api.client.app  # type: ignore[attr-defined]
        for _ in range(100):  # the handler notices the close and unregisters
            if not app.state.registry.connections():
                break
            await asyncio.sleep(0.05)
        async with app.state.sessionmaker() as db:
            await db.execute(delete(NodeConnection))
            await db.commit()
        return await app.state.cluster.restore_connections()  # type: ignore[no-any-return]

    assert _run(api, settle_and_lose_rows) == 0
    assert _rows(api) == []
