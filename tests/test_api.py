from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ipping_backend.main import create_app

from .conftest import PASSWORD, SETUP_TOKEN, Api, hello, node_headers
from .fakes import fake_resolver


def run_node(ws: Any, results: list[int | None]) -> dict[str, Any]:
    """Play a well-behaved node: take one task and answer it. `None` means a timed-out echo."""
    task = json.loads(ws.receive_text())
    assert task["type"] == "task"
    ids = {"task_id": task["task_id"], "attempt_id": task["attempt_id"]}
    ws.send_text(json.dumps({"type": "task_ack", **ids}))
    for seq, rtt in enumerate(results):
        frame = {"type": "sample", **ids, "seq": seq}
        frame.update({"status": "ok", "rtt_us": rtt} if rtt is not None else {"status": "timeout"})
        ws.send_text(json.dumps(frame))
    ws.send_text(
        json.dumps(
            {
                "type": "task_result",
                **ids,
                "status": "completed",
                "tested_ip": task["target"].get("ip"),
            }
        )
    )
    barrier(ws)
    return task


def barrier(ws: Any) -> None:
    """Frames from one node are handled in order, so a pong proves the earlier ones are done."""
    ws.send_text(json.dumps({"type": "ping"}))
    assert json.loads(ws.receive_text())["type"] == "pong"


def read_sse(response: Any) -> list[dict[str, Any]]:
    events, current = [], {}
    for line in response.iter_lines():
        if line == "":
            if current:
                events.append(current)
                current = {}
        elif not line.startswith((":", "retry:")):
            key, _, value = line.partition(": ")
            current[key] = value
    return events


def connect(
    api: Api, credential: str, ipv6: bool = True, kinds: tuple[str, ...] = ("ping",)
) -> Any:
    ws = api.client.websocket_connect("/api/v1/nodes/connect", headers=node_headers(credential))
    session = ws.__enter__()
    session.send_text(hello(ipv6=ipv6, kinds=kinds))
    welcome = json.loads(session.receive_text())
    assert welcome["type"] == "welcome"
    return ws, session


# ---------------------------------------------------------------------------------------------
# Setup, sessions, CSRF
# ---------------------------------------------------------------------------------------------


def test_first_run_setup_and_login(api: Api) -> None:
    api.start_session()
    assert api.get("/api/v1/setup/status").json() == {"needs_setup": True}
    bad = api.post(
        "/api/v1/setup", {"setup_token": "wrong", "username": "admin", "password": PASSWORD}
    )
    assert bad.status_code == 403
    short = api.post(
        "/api/v1/setup", {"setup_token": SETUP_TOKEN, "username": "admin", "password": "short"}
    )
    assert short.status_code == 422

    old_cookie = api.client.cookies.get("ipping_session")
    api.become_admin()
    assert api.client.cookies.get("ipping_session") != old_cookie  # session rotated on sign-in
    assert api.get("/api/v1/setup/status").json() == {"needs_setup": False}
    assert api.get("/api/v1/admin/me").json()["username"] == "admin"

    again = api.post(
        "/api/v1/setup", {"setup_token": SETUP_TOKEN, "username": "x" * 5, "password": PASSWORD}
    )
    assert again.status_code == 409

    api.post("/api/v1/admin/logout")
    api.start_session()
    assert api.get("/api/v1/admin/me").status_code == 401
    wrong = api.post("/api/v1/admin/login", {"username": "admin", "password": "nope"})
    assert wrong.status_code == 401
    ok = api.post("/api/v1/admin/login", {"username": "admin", "password": PASSWORD})
    assert ok.status_code == 200 and ok.json()["authenticated"]


def test_writes_need_the_csrf_token_and_a_same_site_origin(api: Api) -> None:
    api.become_admin()
    no_token = api.client.post("/api/v1/admin/enrollment-tokens", json={})
    assert no_token.status_code == 403 and no_token.json()["error"]["code"] == "bad_csrf_token"
    foreign = api.post(
        "/api/v1/admin/enrollment-tokens", {}, headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403 and foreign.json()["error"]["code"] == "bad_origin"
    same = api.post("/api/v1/admin/enrollment-tokens", {}, headers={"Origin": "http://testserver"})
    assert same.status_code == 201


def test_admin_endpoints_require_admin(api: Api) -> None:
    api.start_session()
    assert api.get("/api/v1/admin/nodes").status_code == 401
    assert api.get("/api/v1/admin/settings").status_code == 401


# ---------------------------------------------------------------------------------------------
# Node enrollment and connection
# ---------------------------------------------------------------------------------------------


def test_enrollment_token_is_single_use_and_node_needs_approval(api: Api) -> None:
    api.become_admin()
    token = api.post("/api/v1/admin/enrollment-tokens", {"label": "x"}).json()["token"]
    body = {"token": token, "name": "n1", "version": "0.1.0"}
    first = api.client.post("/api/v1/nodes/enroll", json=body)
    assert first.status_code == 201 and first.json()["status"] == "pending"
    assert api.client.post("/api/v1/nodes/enroll", json=body).status_code == 403
    assert (
        api.client.post(
            "/api/v1/nodes/enroll", json={**body, "token": "ipt_" + "x" * 40}
        ).status_code
        == 403
    )

    assert api.get("/api/v1/nodes").json() == []  # pending nodes are not public
    node_id = first.json()["node_id"]
    api.post(f"/api/v1/admin/nodes/{node_id}/approve")
    public = api.get("/api/v1/nodes").json()
    assert [n["id"] for n in public] == [node_id]
    assert "remote_addr" not in public[0] and public[0]["online"] is False


def test_node_websocket_authentication(api: Api) -> None:
    from starlette.websockets import WebSocketDisconnect

    api.become_admin()
    node_id, credential = api.enroll_node()
    for headers in (
        {},
        node_headers("nope"),
        node_headers(f"{node_id}.wrong"),
        node_headers(f"missing.{credential.split('.')[1]}"),
    ):
        with (
            pytest.raises(WebSocketDisconnect),
            api.client.websocket_connect("/api/v1/nodes/connect", headers=headers),
        ):
            pass

    ws, session = connect(api, credential)
    try:
        assert api.get("/api/v1/nodes").json()[0]["online"] is True
        session.send_text(json.dumps({"type": "ping"}))
        assert json.loads(session.receive_text())["type"] == "pong"
        session.send_text("this is not json")
        assert json.loads(session.receive_text())["code"] == "invalid_frame"
    finally:
        ws.__exit__(None, None, None)

    # Revoking cuts a node off for good.
    api.post(f"/api/v1/admin/nodes/{node_id}/revoke")
    with (
        pytest.raises(WebSocketDisconnect),
        api.client.websocket_connect("/api/v1/nodes/connect", headers=node_headers(credential)),
    ):
        pass


def test_unsupported_protocol_version_is_refused(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    with api.client.websocket_connect(
        "/api/v1/nodes/connect", headers=node_headers(credential)
    ) as ws:
        ws.send_text(
            json.dumps(
                {
                    "type": "hello",
                    "protocol_versions": [99],
                    "node_version": "9",
                    "capabilities": {},
                }
            )
        )
        assert json.loads(ws.receive_text())["code"] == "unsupported_protocol"


# ---------------------------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------------------------


def test_full_measurement_through_a_node(api: Api) -> None:
    api.become_admin()
    node_id, credential = api.enroll_node()
    ws, session = connect(api, credential)
    try:
        created = api.post(
            "/api/v1/measurements", {"target": "https://user:pw@example.com/a/b?secret=1"}
        )
        assert created.status_code == 201, created.text
        measurement = created.json()
        assert measurement["target"] == {
            "host": "example.com",
            "ip": "93.184.216.34",
            "resolved_ips": ["93.184.216.34"],
            "input_kind": "url",
        }
        assert "secret" not in json.dumps(measurement) and "pw" not in measurement["target"]["host"]
        assert [t["node"]["id"] for t in measurement["tasks"]] == [node_id]

        task = run_node(session, [10_000, None, 30_000, 20_000])
        assert task["target"] == {"ip": "93.184.216.34", "family": "auto"}
        assert task["options"] == {"count": 4, "interval_ms": 1000, "timeout_ms": 2000}

        final = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        assert final["status"] == "completed"
        done = final["tasks"][0]
        assert done["status"] == "completed"
        assert done["summary"] == {
            "sent": 4,
            "received": 3,
            "loss_pct": 25.0,
            "min_ms": 10.0,
            "median_ms": 20.0,
            "avg_ms": 20.0,
            "max_ms": 30.0,
            "reached": None,
        }
        assert [s["status"] for s in done["samples"]] == ["ok", "timeout", "ok", "ok"]
        assert final["stats"]["nodes_responding"] == 1
        assert final["stats"]["fastest_task_id"] == done["id"]
    finally:
        ws.__exit__(None, None, None)

    # The live stream replays the whole history, and resumes from Last-Event-ID after a drop.
    with api.client.stream("GET", f"/api/v1/measurements/{measurement['id']}/events") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        events = read_sse(response)
    types = [e["event"] for e in events]
    assert types[0] == "measurement.created" and types[-1] == "measurement.finished"
    assert types.count("sample") == 4
    assert [int(e["id"]) for e in events] == list(range(1, len(events) + 1))

    cut = int(events[3]["id"])
    with api.client.stream(
        "GET",
        f"/api/v1/measurements/{measurement['id']}/events",
        headers={"Last-Event-ID": str(cut)},
    ) as response:
        resumed = read_sse(response)
    assert [e["id"] for e in resumed] == [e["id"] for e in events[cut:]]


def test_unsafe_and_invalid_targets_are_refused(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential)
    try:
        for target, code in [
            ("127.0.0.1", "blocked_by_policy"),
            ("http://169.254.169.254/latest/meta-data", "blocked_by_policy"),
            ("10.0.0.1", "blocked_by_policy"),
            ("internal.example.com", "blocked_by_policy"),
            ("mixed.example.com", "blocked_by_policy"),
            ("localhost", "blocked_by_policy"),
            ("nosuchhost.example.com", "dns_failure"),
            ("ftp://example.com", "unsupported_scheme"),
            ("", "validation_error"),
        ]:
            response = api.post("/api/v1/measurements", {"target": target})
            assert response.status_code == 422, (target, response.text)
            assert response.json()["error"]["code"] == code, target
    finally:
        ws.__exit__(None, None, None)


def test_no_measurement_without_an_eligible_node(api: Api) -> None:
    api.become_admin()
    none_online = api.post("/api/v1/measurements", {"target": "example.com"})
    assert (
        none_online.status_code == 409
        and none_online.json()["error"]["code"] == "no_eligible_nodes"
    )

    node_id, credential = api.enroll_node()
    ws, _ = connect(api, credential, ipv6=False)  # IPv4-only node
    try:
        v6 = api.post("/api/v1/measurements", {"target": "2606:4700:4700::1111"})
        assert v6.status_code == 409 and v6.json()["error"]["code"] == "no_eligible_nodes"
        explicit = api.post(
            "/api/v1/measurements", {"target": "example.com", "node_ids": [node_id, "ghost"]}
        )
        assert (
            explicit.status_code == 409 and explicit.json()["error"]["code"] == "node_unavailable"
        )
    finally:
        ws.__exit__(None, None, None)


def test_pending_nodes_never_receive_work(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node(approve=False)
    ws, _ = connect(api, credential)
    try:
        response = api.post("/api/v1/measurements", {"target": "example.com"})
        assert response.json()["error"]["code"] == "no_eligible_nodes"
    finally:
        ws.__exit__(None, None, None)


def test_budget_and_emergency_stop(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential)
    try:
        assert (
            api.patch("/api/v1/admin/settings", {"max_probes_per_measurement": 3}).status_code
            == 200
        )
        over = api.post("/api/v1/measurements", {"target": "example.com", "count": 4})
        assert over.status_code == 422 and over.json()["error"]["code"] == "probe_budget_exceeded"

        api.patch(
            "/api/v1/admin/settings",
            {"max_probes_per_measurement": 1000, "measurements_enabled": False},
        )
        stopped = api.post("/api/v1/measurements", {"target": "example.com"})
        assert (
            stopped.status_code == 503
            and stopped.json()["error"]["code"] == "measurements_disabled"
        )
    finally:
        ws.__exit__(None, None, None)


def test_concurrent_measurement_limit_per_session(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential)
    try:
        assert api.post("/api/v1/measurements", {"target": "example.com"}).status_code == 201
        assert api.post("/api/v1/measurements", {"target": "example.com"}).status_code == 201
        third = api.post("/api/v1/measurements", {"target": "example.com"})
        assert third.status_code == 429 and third.json()["error"]["code"] == "too_many_running"
    finally:
        ws.__exit__(None, None, None)


def test_node_disconnect_ends_its_tasks_without_counting_loss(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential)
    measurement = api.post("/api/v1/measurements", {"target": "example.com"}).json()
    task = json.loads(session.receive_text())
    session.send_text(
        json.dumps(
            {
                "type": "sample",
                "task_id": task["task_id"],
                "attempt_id": task["attempt_id"],
                "seq": 0,
                "status": "ok",
                "rtt_us": 5000,
            }
        )
    )
    barrier(session)
    ws.__exit__(None, None, None)  # the node drops mid-measurement

    for _ in range(50):
        final = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        if final["status"] != "running":
            break
        api.client.portal.call(__import__("asyncio").sleep, 0.05)
    assert final["status"] == "completed"
    assert final["tasks"][0]["status"] == "node_disconnected"
    assert final["tasks"][0]["error_code"] == "node_disconnected"
    assert final["stats"]["nodes_failed"] == 1


def test_deadline_times_out_silent_nodes(app: Any, api: Api) -> None:
    from datetime import timedelta

    from sqlalchemy import update

    from ipping_backend.models import Measurement, utcnow

    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential)
    try:
        measurement = api.post("/api/v1/measurements", {"target": "example.com"}).json()
        json.loads(session.receive_text())  # the node got the task and says nothing

        async def let_the_deadline_pass() -> None:
            async with app.state.sessionmaker() as db:
                await db.execute(
                    update(Measurement).values(deadline_at=utcnow() - timedelta(seconds=1))
                )
                await db.commit()
            await app.state.engine.reap()

        api.client.portal.call(let_the_deadline_pass)
        final = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        assert final["status"] == "completed"
        assert final["tasks"][0]["status"] == "timed_out"
        assert json.loads(session.receive_text())["type"] == "cancel"
    finally:
        ws.__exit__(None, None, None)


def test_cancel(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential)
    try:
        measurement = api.post("/api/v1/measurements", {"target": "example.com"}).json()
        task = json.loads(session.receive_text())
        assert api.post(f"/api/v1/measurements/{measurement['id']}/cancel").status_code == 202
        assert json.loads(session.receive_text()) == {"type": "cancel", "task_id": task["task_id"]}
        final = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        assert (final["status"], final["tasks"][0]["status"]) == ("cancelled", "cancelled")
        # A late result from the node cannot revive a cancelled task.
        session.send_text(
            json.dumps(
                {
                    "type": "task_result",
                    "task_id": task["task_id"],
                    "attempt_id": task["attempt_id"],
                    "status": "completed",
                }
            )
        )
        barrier(session)
        again = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        assert again["tasks"][0]["status"] == "cancelled"
    finally:
        ws.__exit__(None, None, None)


def test_results_are_private_until_shared(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential)
    try:
        mid = api.post("/api/v1/measurements", {"target": "example.com"}).json()["id"]
        owner_cookies = dict(api.client.cookies)

        api.client.cookies.clear()
        stranger = Api(api.client)
        stranger.start_session()
        assert stranger.get(f"/api/v1/measurements/{mid}").status_code == 404
        assert (
            stranger.post(f"/api/v1/measurements/{mid}/share", {"shared": True}).status_code == 404
        )

        api.client.cookies.clear()
        api.client.cookies.update(owner_cookies)
        assert api.post(f"/api/v1/measurements/{mid}/share", {"shared": True}).json() == {
            "shared": True
        }

        api.client.cookies.clear()
        stranger.start_session()
        assert stranger.get(f"/api/v1/measurements/{mid}").status_code == 200
        assert stranger.post(f"/api/v1/measurements/{mid}/cancel").status_code == 404  # read-only
    finally:
        ws.__exit__(None, None, None)


def test_regional_dns_mode_sends_the_hostname_to_the_node(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential)
    try:
        measurement = api.post(
            "/api/v1/measurements", {"target": "example.com", "dns_mode": "regional", "family": "4"}
        ).json()
        assert measurement["target"]["ip"] is None and measurement["dns_mode"] == "regional"
        task = json.loads(session.receive_text())
        assert task["target"] == {"hostname": "example.com", "family": "4"}
        ids = {"task_id": task["task_id"], "attempt_id": task["attempt_id"]}
        session.send_text(
            json.dumps({"type": "sample", **ids, "seq": 0, "status": "ok", "rtt_us": 1000})
        )
        session.send_text(
            json.dumps(
                {
                    "type": "task_result",
                    **ids,
                    "status": "completed",
                    "tested_ip": "93.184.216.34",
                    "resolved_ips": ["93.184.216.34", "10.0.0.1", "garbage"],
                }
            )
        )
        barrier(session)
        final = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        assert final["tasks"][0]["tested_ip"] == "93.184.216.34"
        assert final["tasks"][0]["resolved_ips"] == [
            "93.184.216.34"
        ]  # non-public / invalid entries dropped
    finally:
        ws.__exit__(None, None, None)


def test_fixed_mode_rejects_a_node_that_probed_a_different_address(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential)
    try:
        measurement = api.post("/api/v1/measurements", {"target": "example.com"}).json()
        task = json.loads(session.receive_text())
        session.send_text(
            json.dumps(
                {
                    "type": "task_result",
                    "task_id": task["task_id"],
                    "attempt_id": task["attempt_id"],
                    "status": "completed",
                    "tested_ip": "8.8.8.8",
                }
            )
        )
        barrier(session)
        final = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        assert final["tasks"][0]["status"] == "failed"
    finally:
        ws.__exit__(None, None, None)


def test_duplicate_and_forged_frames_are_ignored(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential)
    try:
        measurement = api.post("/api/v1/measurements", {"target": "example.com"}).json()
        task = json.loads(session.receive_text())
        ids = {"task_id": task["task_id"], "attempt_id": task["attempt_id"]}
        sample = json.dumps({"type": "sample", **ids, "seq": 0, "status": "ok", "rtt_us": 7000})
        session.send_text(sample)
        session.send_text(sample)  # retransmission
        session.send_text(
            json.dumps({"type": "sample", **ids, "seq": 9, "status": "ok", "rtt_us": 1})
        )  # beyond count
        session.send_text(
            json.dumps(
                {
                    "type": "sample",
                    "task_id": task["task_id"],
                    "attempt_id": "forged",
                    "seq": 1,
                    "status": "ok",
                    "rtt_us": 1,
                }
            )
        )
        barrier(session)  # everything above has been processed
        current = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        assert len(current["tasks"][0]["samples"]) == 1
    finally:
        ws.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------------------------


def test_backend_restart_interrupts_running_measurements(settings: Any) -> None:
    first = TestClient(create_app(settings, resolver=fake_resolver, create_schema=True))
    with first:
        api = Api(first)
        api.become_admin()
        _, credential = api.enroll_node()
        ws, session = connect(api, credential)
        mid = api.post("/api/v1/measurements", {"target": "example.com"}).json()["id"]
        json.loads(session.receive_text())
        cookies, csrf = dict(first.cookies), api.csrf
        ws.__exit__(None, None, None)

    # Same database, new process: the old measurement must be closed out, not left "running".
    second = TestClient(
        create_app(settings, resolver=fake_resolver, create_schema=True), cookies=cookies
    )
    with second:
        final = second.get(f"/api/v1/measurements/{mid}", headers={"X-CSRF-Token": csrf}).json()
    assert final["status"] == "interrupted"
    assert final["tasks"][0]["status"] in ("interrupted", "node_disconnected")


def test_retention_removes_old_samples_but_keeps_summaries(app: Any, api: Api) -> None:
    from datetime import timedelta

    from sqlalchemy import func, select, update

    from ipping_backend import maintenance
    from ipping_backend.models import Measurement, Sample, utcnow

    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential)
    try:
        mid = api.post("/api/v1/measurements", {"target": "example.com"}).json()["id"]
        run_node(session, [1000, 2000, 3000, 4000])
        api.get(f"/api/v1/measurements/{mid}")
    finally:
        ws.__exit__(None, None, None)

    async def age_and_sweep() -> tuple[dict[str, int], int]:
        sm = app.state.sessionmaker
        async with sm() as db:
            await db.execute(update(Measurement).values(created_at=utcnow() - timedelta(days=10)))
            await db.commit()
        removed = await maintenance.run_once(sm)
        async with sm() as db:
            return removed, (await db.scalar(select(func.count()).select_from(Sample))) or 0

    removed, remaining = api.client.portal.call(age_and_sweep)
    assert removed["samples"] == 4 and remaining == 0
    final = api.get(f"/api/v1/measurements/{mid}").json()
    assert final["tasks"][0]["summary"]["received"] == 4 and final["tasks"][0]["samples"] == []


def test_maintenance_forgets_instances_that_stopped_long_ago(app: Any, api: Api) -> None:
    from datetime import timedelta

    from sqlalchemy import select

    from ipping_backend import maintenance
    from ipping_backend.models import BackendInstance, utcnow

    async def run() -> list[str]:
        sm = app.state.sessionmaker
        async with sm() as db:
            db.add(
                BackendInstance(
                    id="dead" + "0" * 28, version="0", heartbeat_at=utcnow() - timedelta(days=2)
                )
            )
            await db.commit()
        await maintenance.run_once(sm)
        async with sm() as db:
            return list((await db.execute(select(BackendInstance.id))).scalars())

    remaining = api.client.portal.call(run)
    assert app.state.cluster.instance_id in remaining  # the running one is never touched
    assert not any(i.startswith("dead") for i in remaining)
