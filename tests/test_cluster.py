"""Two backend instances sharing one PostgreSQL database.

Each test runs two complete applications (own connections, own dispatcher, own reaper) against the
same database and checks that they behave as one service: a measurement created through either one
reaches a node connected to the other, results stream from either, commands cross over, and a crashed
instance is noticed and cleaned up after. They need PostgreSQL (`IPPING_TEST_DATABASE_URL`), because
that is what makes processes share state and serialise on row locks.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from ipping_backend.config import Settings
from ipping_backend.main import create_app
from ipping_backend.models import MeasurementTask

from .conftest import Api, node_headers
from .fakes import fake_resolver
from .test_api import barrier, connect, read_sse
from .test_location_api import answer

pytestmark = pytest.mark.skipif(
    "IPPING_TEST_DATABASE_URL" not in os.environ,
    reason="needs PostgreSQL: set IPPING_TEST_DATABASE_URL",
)


@dataclass
class Pair:
    a: Api
    b: Api
    app_a: Any
    app_b: Any

    def other_task_owner(self, task_id: str) -> str | None:
        async def owner() -> str | None:
            async with self.app_a.state.sessionmaker() as db:
                return (
                    await db.execute(
                        select(MeasurementTask.owner_instance).where(MeasurementTask.id == task_id)
                    )
                ).scalar_one_or_none()

        return self.a.client.portal.call(owner)  # type: ignore[no-any-return]


@pytest.fixture
def pair(settings: Settings) -> Iterator[Pair]:
    fast = settings.model_copy(
        update={
            "instance_heartbeat_s": 0.2,
            "instance_dead_after_s": 1.0,
            "reaper_interval_s": 0.2,
            "dispatch_poll_s": 0.2,
            "pending_grace_s": 1.5,
        }
    )
    app_a = create_app(fast, resolver=fake_resolver, create_schema=True)
    app_b = create_app(fast, resolver=fake_resolver, create_schema=True)
    with TestClient(app_a) as client_a, TestClient(app_b) as client_b:
        a, b = Api(client_a), Api(client_b)
        a.become_admin()
        client_b.cookies.update(
            client_a.cookies
        )  # the same browser session, seen by the other instance
        b.csrf = a.csrf
        yield Pair(a, b, app_a, app_b)


def wait_for(check: Any, timeout: float = 8.0, step: float = 0.1) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(step)
    return check()


def test_a_measurement_created_on_one_instance_runs_on_a_node_held_by_the_other(pair: Pair) -> None:
    _, credential = pair.a.enroll_node("held by A")
    ws, session = connect(pair.a, credential)
    try:
        created = pair.b.post("/api/v1/measurements", {"target": "example.com"})
        assert created.status_code == 201, created.text
        measurement = created.json()

        answer(session, 12.0)  # the task arrives on A's socket although B created it

        final = wait_for(
            lambda: (
                (m := pair.b.get(f"/api/v1/measurements/{measurement['id']}").json())["status"]
                == "completed"
                and m
            )
        )
        assert final["tasks"][0]["summary"]["received"] == 4

        # The sending instance is recorded on the task: it is A, not B.
        assert (
            pair.other_task_owner(measurement["tasks"][0]["id"])
            == pair.app_a.state.cluster.instance_id
        )
        assert pair.app_a.state.cluster.instance_id != pair.app_b.state.cluster.instance_id

        # Both instances report the node online, and both can stream the finished measurement.
        for api in (pair.a, pair.b):
            assert api.get("/api/v1/nodes").json()[0]["online"] is True
            with api.client.stream(
                "GET", f"/api/v1/measurements/{measurement['id']}/events"
            ) as response:
                events = read_sse(response)
            assert [e["event"] for e in events][0] == "measurement.created"
            assert events[-1]["event"] == "measurement.finished"
            assert [int(e["id"]) for e in events] == list(range(1, len(events) + 1))
    finally:
        ws.__exit__(None, None, None)


def test_a_live_stream_on_one_instance_sees_events_produced_on_the_other_promptly(
    pair: Pair,
) -> None:
    _, credential = pair.a.enroll_node()
    ws, session = connect(pair.a, credential)
    try:
        measurement = pair.b.post("/api/v1/measurements", {"target": "example.com"}).json()
        seen: dict[str, float] = {}

        def follow() -> None:
            with pair.b.client.stream(
                "GET", f"/api/v1/measurements/{measurement['id']}/events"
            ) as response:
                event = ""
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        event = line[7:]
                        seen[event] = time.monotonic()
                    if event == "measurement.finished":
                        return

        reader = threading.Thread(target=follow, daemon=True)
        reader.start()
        time.sleep(0.5)  # the stream is open and idle
        answered = time.monotonic()
        answer(session, 9.0)
        reader.join(timeout=10)
        assert not reader.is_alive(), "the stream never saw the measurement finish"

        # The stream polls every 3 s, so seeing the end sooner than that means the PostgreSQL
        # notification woke it.
        assert seen["measurement.finished"] - answered < 2.0
    finally:
        ws.__exit__(None, None, None)


def test_cancel_and_revoke_reach_a_node_connected_to_the_other_instance(pair: Pair) -> None:
    node_id, credential = pair.a.enroll_node()
    ws, session = connect(pair.a, credential)
    try:
        measurement = pair.b.post("/api/v1/measurements", {"target": "example.com"}).json()
        task = json.loads(session.receive_text())
        assert pair.b.post(f"/api/v1/measurements/{measurement['id']}/cancel").status_code == 202
        assert json.loads(session.receive_text()) == {"type": "cancel", "task_id": task["task_id"]}

        # Revoking on B must close the socket held by A.
        assert pair.b.post(f"/api/v1/admin/nodes/{node_id}/revoke").status_code == 200
        with pytest.raises(WebSocketDisconnect) as closed:
            for _ in range(50):
                session.receive_text()
        assert closed.value.code == 4403
    finally:
        ws.__exit__(None, None, None)


def test_a_node_that_reconnects_to_the_other_instance_replaces_its_old_connection(
    pair: Pair,
) -> None:
    _, credential = pair.a.enroll_node()
    old = pair.a.client.websocket_connect("/api/v1/nodes/connect", headers=node_headers(credential))
    old_session = old.__enter__()
    old_session.send_text(
        json.dumps(
            {
                "type": "hello",
                "protocol_versions": [1],
                "node_version": "0.1.0",
                "capabilities": {"kinds": ["ping"], "ipv4": True, "ipv6": False},
            }
        )
    )
    old_session.receive_text()  # welcome
    new, new_session = connect(pair.b, credential)
    try:
        with pytest.raises(WebSocketDisconnect) as closed:
            for _ in range(50):
                old_session.receive_text()
        assert closed.value.code == 4409

        # New work goes to the connection that is current.
        created = pair.a.post("/api/v1/measurements", {"target": "example.com"})
        assert created.status_code == 201, created.text
        measurement = created.json()
        task = json.loads(new_session.receive_text())
        assert task["type"] == "task"
        answer_frames = {"task_id": task["task_id"], "attempt_id": task["attempt_id"]}
        new_session.send_text(
            json.dumps(
                {
                    "type": "task_result",
                    **answer_frames,
                    "status": "failed",
                    "error_code": "dns_failure",
                }
            )
        )
        barrier(new_session)
        assert wait_for(
            lambda: (
                pair.a.get(f"/api/v1/measurements/{measurement['id']}").json()["status"]
                == "completed"
            )
        )
    finally:
        new.__exit__(None, None, None)
        old.__exit__(None, None, None)


def crash(pair: Pair) -> None:
    """Instance A "crashes": it stops heartbeating without saying goodbye, as a killed process would."""

    async def stop() -> None:
        # Everything that makes A a living instance: its heartbeat, dispatcher and reaper.
        for task in [*pair.app_a.state.cluster._tasks, *pair.app_a.state.loops]:
            task.cancel()

    pair.a.client.portal.call(stop)


def settled(pair: Pair, measurement_id: str, timeout: float = 12.0) -> Any:
    return wait_for(
        lambda: (
            (m := pair.b.get(f"/api/v1/measurements/{measurement_id}").json())["status"]
            != "running"
            and m
        ),
        timeout=timeout,
    )


def test_when_an_instance_dies_a_task_whose_node_never_comes_back_is_a_node_failure(
    pair: Pair,
) -> None:
    _, credential = pair.a.enroll_node()
    ws, session = connect(pair.a, credential)
    try:
        measurement = pair.b.post("/api/v1/measurements", {"target": "example.com"}).json()
        json.loads(session.receive_text())  # A sent the task; the node never answers
        crash(pair)

        # The task is sent again, but its node does not reconnect, so after the grace period it is
        # the node that is blamed, not the target and not the backend.
        final = settled(pair, measurement["id"])
        assert final["tasks"][0]["status"] == "node_disconnected"
        assert final["tasks"][0]["attempts"] == 2

        # And its node no longer counts as online, so nothing new is promised to it.
        refused = pair.b.post("/api/v1/measurements", {"target": "example.com"})
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "no_eligible_nodes"
    finally:
        ws.__exit__(None, None, None)


def test_a_task_the_node_had_not_started_is_sent_again_when_its_instance_dies(pair: Pair) -> None:
    _, credential = pair.a.enroll_node()
    first, first_session = connect(pair.a, credential)
    try:
        measurement = pair.b.post("/api/v1/measurements", {"target": "example.com"}).json()
        original = json.loads(first_session.receive_text())
        crash(pair)

        # The node reconnects, to the instance that is still alive, and is given the task again.
        second, second_session = connect(pair.b, credential)
        try:
            again = json.loads(second_session.receive_text())
            assert again["type"] == "task" and again["task_id"] == original["task_id"]
            assert again["attempt_id"] != original["attempt_id"], (
                "a new attempt, so old frames lose"
            )

            # A late frame from the first attempt is ignored.
            second_session.send_text(
                json.dumps(
                    {
                        "type": "sample",
                        "task_id": original["task_id"],
                        "attempt_id": original["attempt_id"],
                        "seq": 0,
                        "status": "ok",
                        "rtt_us": 1,
                    }
                )
            )
            barrier(second_session)
            answer_frames = {"task_id": again["task_id"], "attempt_id": again["attempt_id"]}
            for seq, rtt in enumerate([10_000, 20_000, 30_000, 40_000]):
                second_session.send_text(
                    json.dumps(
                        {
                            "type": "sample",
                            **answer_frames,
                            "seq": seq,
                            "status": "ok",
                            "rtt_us": rtt,
                        }
                    )
                )
            second_session.send_text(
                json.dumps(
                    {
                        "type": "task_result",
                        **answer_frames,
                        "status": "completed",
                        "tested_ip": again["target"]["ip"],
                    }
                )
            )
            barrier(second_session)

            final = settled(pair, measurement["id"])
            assert final["status"] == "completed"
            task = final["tasks"][0]
            assert task["status"] == "completed" and task["attempts"] == 2
            assert [s["rtt_ms"] for s in task["samples"]] == [10.0, 20.0, 30.0, 40.0]
            with pair.b.client.stream(
                "GET", f"/api/v1/measurements/{measurement['id']}/events"
            ) as response:
                updates = [
                    json.loads(e["data"])["status"]
                    for e in read_sse(response)
                    if e["event"] == "task.updated"
                ]
            assert "pending" in updates[1:], f"the retry should be visible: {updates}"
        finally:
            second.__exit__(None, None, None)
    finally:
        first.__exit__(None, None, None)


def test_a_task_that_already_produced_results_is_interrupted_and_keeps_them(pair: Pair) -> None:
    _, credential = pair.a.enroll_node()
    ws, session = connect(pair.a, credential)
    try:
        measurement = pair.b.post("/api/v1/measurements", {"target": "example.com"}).json()
        task = json.loads(session.receive_text())
        frames = {"task_id": task["task_id"], "attempt_id": task["attempt_id"]}
        for seq in range(2):
            session.send_text(
                json.dumps({"type": "sample", **frames, "seq": seq, "status": "ok", "rtt_us": 5000})
            )
        barrier(session)
        crash(pair)

        final = settled(pair, measurement["id"])
        done = final["tasks"][0]
        assert final["status"] == "interrupted" and done["status"] == "interrupted"
        assert done["attempts"] == 1, "starting over would have thrown the two results away"
        assert [s["status"] for s in done["samples"]] == ["ok", "ok"]
    finally:
        ws.__exit__(None, None, None)


def test_retries_can_be_turned_off(pair: Pair) -> None:
    assert pair.b.patch("/api/v1/admin/settings", {"task_retries": 0}).status_code == 200
    _, credential = pair.a.enroll_node()
    ws, session = connect(pair.a, credential)
    try:
        measurement = pair.b.post("/api/v1/measurements", {"target": "example.com"}).json()
        json.loads(session.receive_text())
        crash(pair)
        final = settled(pair, measurement["id"])
        assert final["status"] == "interrupted"
        assert final["tasks"][0]["status"] == "interrupted" and final["tasks"][0]["attempts"] == 1
    finally:
        ws.__exit__(None, None, None)


def test_a_task_is_not_sent_again_more_than_the_limit_allows(pair: Pair) -> None:
    _, credential = pair.a.enroll_node()
    ws, session = connect(pair.a, credential)
    try:
        measurement = pair.b.post("/api/v1/measurements", {"target": "example.com"}).json()
        json.loads(session.receive_text())
        task_id = measurement["tasks"][0]["id"]

        async def already_retried() -> None:
            async with pair.app_a.state.sessionmaker() as db:
                task = await db.get(MeasurementTask, task_id)
                assert task is not None
                task.attempts = 2  # one retry has been used and the limit is one
                await db.commit()

        pair.a.client.portal.call(already_retried)
        crash(pair)
        final = settled(pair, measurement["id"])
        assert final["tasks"][0]["status"] == "interrupted"
    finally:
        ws.__exit__(None, None, None)


def test_a_task_waiting_for_a_node_is_sent_when_that_node_connects_anywhere(pair: Pair) -> None:
    node_id, credential = pair.a.enroll_node()
    first, first_session = connect(pair.a, credential)
    measurement = pair.b.post("/api/v1/measurements", {"target": "example.com"}).json()
    json.loads(first_session.receive_text())  # sent on A, never answered
    first.__exit__(None, None, None)  # A's node drops mid-task

    assert wait_for(
        lambda: (
            pair.b.get(f"/api/v1/measurements/{measurement['id']}").json()["tasks"][0]["status"]
            == "node_disconnected"
        )
    )
    # With nothing outstanding, a node connecting to the *other* instance is picked up for new work.
    second, second_session = connect(pair.b, credential)
    try:
        again = pair.a.post("/api/v1/measurements", {"target": "example.com"})
        assert again.status_code == 201
        assert json.loads(second_session.receive_text())["type"] == "task"
    finally:
        second.__exit__(None, None, None)


def test_rate_limits_are_counted_across_instances(pair: Pair) -> None:
    # Five setup attempts per minute are allowed per address, and the fixture's own sign-up already
    # used one. Alternating between the two instances, the fifth further attempt must be refused:
    # per-process counters would never get there.
    attempt = {
        "setup_token": "wrong",
        "username": "second-admin",
        "password": "another long password",
    }
    statuses = []
    for api in (pair.a, pair.b, pair.a, pair.b, pair.a, pair.b):
        api.start_session()
        statuses.append(api.post("/api/v1/setup", attempt).status_code)
    assert statuses[:4] == [409] * 4  # an administrator exists: refused, but each attempt counted
    assert statuses[4:] == [429, 429]


def test_instances_agree_on_the_setup_token_when_none_is_configured(settings: Settings) -> None:
    no_token = settings.model_copy(update={"setup_token": None})
    app_a = create_app(no_token, resolver=fake_resolver, create_schema=True)
    app_b = create_app(no_token, resolver=fake_resolver, create_schema=True)
    with TestClient(app_a), TestClient(app_b) as client_b:
        assert app_a.state.setup_token and app_a.state.setup_token == app_b.state.setup_token
        api = Api(client_b)
        api.start_session()
        done = api.post(
            "/api/v1/setup",
            {
                "setup_token": app_a.state.setup_token,
                "username": "admin",
                "password": "correct horse battery",
            },
        )
        assert done.status_code == 200
        api.csrf = done.json()["csrf_token"]  # signing in replaces the session, and its CSRF token
        # The stored token is gone once an administrator exists.
        again = api.post(
            "/api/v1/setup",
            {
                "setup_token": app_a.state.setup_token,
                "username": "x" * 5,
                "password": "correct horse battery",
            },
        )
        assert again.status_code == 409
