"""Watches: scheduled checks, how a run is judged, when an alert is sent, and who can do what."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from ipping_backend.measurements.schemas import (
    MeasurementOptions,
    MeasurementOut,
    MeasurementStats,
    NodeInfo,
    SampleOut,
    Summary,
    TargetOut,
    TaskOut,
)
from ipping_backend.nodes.protocol import DnsAnswerRecord, DnsDetail, HttpDetail
from ipping_backend.watches import evaluate
from ipping_backend.watches.notify import (
    AlertAddressError,
    check_address,
    deliver,
    make_alert,
    render,
)

from .conftest import Api
from .fakes import fake_resolver
from .test_api import barrier, connect
from .test_probe_kinds import ALL_KINDS, _send, _task

KINDS = (*ALL_KINDS, "dns")


# ---------------------------------------------------------------------------------------------
# How a run is judged: no database, no clock
# ---------------------------------------------------------------------------------------------


def _task_out(
    summary: Summary | None = None,
    samples: list[SampleOut] | None = None,
    status: str = "completed",
) -> TaskOut:
    return TaskOut(
        id="t",
        node=NodeInfo(id="n", name="N"),
        status=status,
        summary=summary,
        samples=samples or [],
    )


def _summary(received: int, sent: int = 4, **extra: Any) -> Summary:
    return Summary(sent=sent, received=received, loss_pct=0.0, **extra)


def _measurement(kind: str, tasks: list[TaskOut], status: str = "completed") -> MeasurementOut:
    return MeasurementOut(
        id="m", kind=kind, status=status,  # type: ignore[arg-type]
        target=TargetOut(host="example.com", ip=None, resolved_ips=[], input_kind="hostname"),
        dns_mode="fixed", family="auto",
        options=MeasurementOptions(count=4, interval_ms=1000, timeout_ms=2000),
        shared=False, created_at="2026-11-07T12:00:00Z", deadline_at="2026-11-07T12:00:30Z",  # type: ignore[arg-type]
        finished_at=None, last_event_seq=1,
        stats=MeasurementStats(nodes_total=len(tasks), nodes_responding=0, nodes_failed=0),
        tasks=tasks,
    )  # fmt: skip


def test_a_node_is_good_when_it_got_what_the_kind_means_by_an_answer() -> None:
    assert evaluate.task_ok("ping", _task_out(_summary(3)))
    assert not evaluate.task_ok("ping", _task_out(_summary(0)))
    assert not evaluate.task_ok("tcp", _task_out(None))

    def http(code: int | None) -> TaskOut:
        return _task_out(samples=[SampleOut(seq=0, status="ok", http=HttpDetail(status_code=code))])

    assert evaluate.task_ok("http", http(200)) and evaluate.task_ok("http", http(404))
    assert not evaluate.task_ok("http", http(503)), "a server error is not up"
    assert not evaluate.task_ok("http", http(None)), "a refused certificate sends no request"

    def dns(rcode: str, n: int) -> TaskOut:
        detail = DnsDetail(
            resolver="system", record_type="A", rcode=rcode,
            answers=[DnsAnswerRecord(name="e.com", type="A", ttl=1, data="1.2.3.4")] * n,
        )  # fmt: skip
        return _task_out(samples=[SampleOut(seq=0, status="ok", dns=detail)])

    assert evaluate.task_ok("dns", dns("NOERROR", 1))
    assert not evaluate.task_ok("dns", dns("NXDOMAIN", 0)), "a name that vanished is a failure"
    assert not evaluate.task_ok("dns", dns("NOERROR", 0)), "no records is a failure too"
    assert evaluate.task_ok("traceroute", _task_out(_summary(5, reached=True)))
    assert not evaluate.task_ok("traceroute", _task_out(_summary(5, reached=False)))


def test_a_run_is_ok_degraded_down_or_an_error() -> None:
    good, bad = _task_out(_summary(4, median_ms=10.0)), _task_out(_summary(0))
    assert evaluate.assess(_measurement("ping", [good] * 5), 80).status == "ok"
    mostly = evaluate.assess(_measurement("ping", [good, good, good, good, bad]), 80)
    assert (mostly.status, mostly.nodes_ok, mostly.nodes_total) == ("ok", 4, 5)  # exactly 80%
    assert (
        evaluate.assess(_measurement("ping", [good, good, good, bad, bad]), 80).status == "degraded"
    )
    assert evaluate.assess(_measurement("ping", [bad, bad]), 80).status == "down"
    assert evaluate.assess(_measurement("ping", []), 80).status == "error"
    interrupted = evaluate.assess(_measurement("ping", [good], status="interrupted"), 80)
    assert interrupted.status == "error" and "interrupted" in (interrupted.note or "")


def test_the_run_records_the_nodes_medians() -> None:
    tasks = [_task_out(_summary(4, median_ms=m)) for m in (5.0, 10.0, 30.0)] + [
        _task_out(_summary(0))
    ]
    result = evaluate.assess(_measurement("ping", tasks), 50)
    assert (result.median_ms, result.min_ms, result.max_ms) == (10.0, 5.0, 30.0)
    assert evaluate.assess(_measurement("ping", [_task_out(_summary(0))]), 50).median_ms is None


@pytest.mark.parametrize(
    ("steps", "events"),
    [
        # bad, bad, bad, bad, good, good: alert once at the second bad run, say so once when it is back
        (
            ["down", "down", "down", "down", "ok", "ok"],
            [None, "alert", None, None, "recovered", None],
        ),
        # one bad run is not enough (alert_after is 2) and a good one resets the count
        (["down", "ok", "down", "ok"], [None, None, None, None]),
        # an error neither counts as bad nor as good
        (["down", "error", "down", "error", "ok"], [None, None, "alert", None, "recovered"]),
        (["degraded", "down", "ok"], [None, "alert", "recovered"]),
    ],
)
def test_alerts_are_sent_once_and_recovery_is_told_once(
    steps: list[str], events: list[str | None]
) -> None:
    bad, alerting, seen = 0, False, []
    for status in steps:
        streak = evaluate.advance(bad, alerting, status, 2)  # type: ignore[arg-type]
        bad, alerting = streak.bad_streak, streak.alerting
        seen.append(streak.event)
    assert seen == events


# ---------------------------------------------------------------------------------------------
# Delivering an alert
# ---------------------------------------------------------------------------------------------

ALERT = make_alert("alert", "w1", "Main site", "http", "example.com", "down", 2, 10, None, "m1")


def test_the_two_formats_say_the_same_thing() -> None:
    headers, body = render(ALERT, "json")
    payload = json.loads(body)
    assert headers["Content-Type"] == "application/json"
    assert payload["event"] == "alert" and payload["target"] == "example.com"
    assert payload["nodes_ok"] == 2 and payload["nodes_total"] == 10
    assert "example.com is not answering" in payload["message"]

    headers, body = render(ALERT, "ntfy")
    assert headers["Title"] == "example.com is not answering" and headers["Priority"] == "high"
    assert headers["Tags"] == "rotating_light" and b"2 of 10 nodes" in body
    back = make_alert("recovered", "w1", "Main", "http", "example.com", "ok", 10, 10, 12.0, "m2")
    headers, _ = render(back, "ntfy")
    assert headers["Priority"] == "default" and headers["Tags"] == "white_check_mark"
    # headers are ASCII, whatever the name of the watch
    odd = make_alert("alert", "w", "Café ☕", "ping", "bücher.example", "down", 0, 3, None, None)
    assert render(odd, "ntfy")[0]["Title"].isascii()


@pytest.mark.parametrize(
    ("url", "private", "problem"),
    [
        ("ftp://example.com/x", False, "http://"),
        ("example.com/hook", False, "http://"),
        ("https://user:pass@example.com/x", False, "credentials"),
        ("http://10.0.0.5/hook", False, "not a public address"),
        ("http://127.0.0.1:8080/hook", False, "not a public address"),
        ("http://[::1]/hook", False, "not a public address"),
        ("http://internal.example.com/hook", False, "not a public address"),  # resolves to 10.0.0.5
        ("https://unknown.example.com/hook", False, "Could not resolve"),
    ],
)
def test_an_alert_address_must_be_public_unless_allowed(
    url: str, private: bool, problem: str
) -> None:
    with pytest.raises(AlertAddressError, match=problem):
        asyncio.run(check_address(url, allow_private=private, resolver=fake_resolver))


def test_a_private_address_can_be_allowed_for_a_webhook_on_your_own_network() -> None:
    host, addresses = asyncio.run(
        check_address("http://10.0.0.5:8080/hook", allow_private=True, resolver=fake_resolver)
    )
    assert (host, addresses) == ("10.0.0.5", ["10.0.0.5"])
    asyncio.run(
        check_address(
            "http://internal.example.com/hook", allow_private=True, resolver=fake_resolver
        )
    )


def test_the_connection_goes_to_the_address_that_was_checked() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    error = asyncio.run(deliver(
        "https://example.com:8443/hook?token=abc", "json", ALERT, allow_private=False,
        resolver=fake_resolver, transport=httpx.MockTransport(handler),
    ))  # fmt: skip
    assert error is None and len(seen) == 1
    request = seen[0]
    # the address that was validated (not a second resolution), with the name kept for Host and TLS
    assert request.url.host == "93.184.216.34" and request.url.port == 8443
    assert request.url.path == "/hook" and request.url.query == b"token=abc"
    assert request.headers["host"] == "example.com:8443"
    assert (
        request.extensions["sni_hostname"] == b"example.com"
        or request.extensions["sni_hostname"] == "example.com"
    )
    assert json.loads(request.content)["watch_name"] == "Main site"


def test_a_failing_or_redirecting_address_is_reported_not_followed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/moved":
            return httpx.Response(302, headers={"Location": "http://10.0.0.5/inside"})
        if request.url.path == "/boom":
            return httpx.Response(500)
        raise httpx.ConnectError("refused")

    kw: dict[str, Any] = {
        "allow_private": False,
        "resolver": fake_resolver,
        "transport": httpx.MockTransport(handler),
    }
    assert (
        asyncio.run(deliver("https://example.com/moved", "json", ALERT, **kw))
        == "The server answered 302."
    )
    assert seen == ["https://93.184.216.34/moved"], "the redirect is not followed"
    assert (
        asyncio.run(deliver("https://example.com/boom", "json", ALERT, **kw))
        == "The server answered 500."
    )
    assert "ConnectError" in (
        asyncio.run(deliver("https://example.com/other", "json", ALERT, **kw)) or ""
    )
    refused = asyncio.run(deliver("http://10.0.0.5/x", "json", ALERT, **kw))
    assert refused is not None and "not a public address" in refused


# ---------------------------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------------------------

BASE = "/api/v1/admin/watches"


def _body(**over: Any) -> dict[str, Any]:
    return {"name": "Main site", "kind": "ping", "target": "example.com", "interval_s": 300, **over}


def test_only_an_administrator_can_use_watches(api: Api) -> None:
    api.start_session()
    assert api.get(BASE).status_code == 401
    assert api.post(BASE, _body()).status_code == 401


def test_a_watch_is_created_listed_changed_and_deleted(api: Api) -> None:
    api.become_admin()
    created = api.post(BASE, _body(count=3, alert_url="https://example.com/hook?token=secret"))
    assert created.status_code == 201, created.text
    watch = created.json()
    assert watch["name"] == "Main site" and watch["enabled"] and watch["next_run_at"] is not None
    assert watch["options"]["count"] == 3 and "port" not in watch["options"]
    assert watch["history"] == []
    assert watch["last_status"] is None and watch["alerting"] is False
    assert api.get(BASE).json()[0]["id"] == watch["id"]

    changed = api.patch(
        f"{BASE}/{watch['id']}", {"interval_s": 600, "min_ok_pct": 50, "enabled": False}
    ).json()
    assert changed["interval_s"] == 600 and changed["min_ok_pct"] == 50
    assert changed["enabled"] is False and changed["next_run_at"] is None
    again = api.patch(f"{BASE}/{watch['id']}", {"enabled": True}).json()
    assert again["next_run_at"] is not None
    cleared = api.patch(f"{BASE}/{watch['id']}", {"alert_url": ""}).json()
    assert cleared["alert_url"] is None
    assert api.patch(f"{BASE}/{watch['id']}", {"all_nodes": True}).json()["node_ids"] is None

    # the audit trail never carries the address: it may hold a secret
    audit = json.dumps(api.get("/api/v1/admin/audit").json())
    assert "watch.create" in audit and "watch.update" in audit and "secret" not in audit

    assert api.request("DELETE", f"{BASE}/{watch['id']}").status_code == 204
    assert api.get(BASE).json() == [] and api.get(f"{BASE}/{watch['id']}").status_code == 404


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"target": "93.184.216.34", "kind": "dns"}, "invalid_target"),
        ({"kind": "tcp"}, "port_required"),
        ({"kind": "tcp", "port": 6379}, "port_not_allowed"),
        ({"kind": "http", "count": 3}, "invalid_option"),
        ({"target": "intranet.corp"}, "blocked_by_policy"),
        ({"interval_s": 30}, "interval_too_short"),
        ({"alert_url": "http://10.0.0.5/hook"}, "invalid_alert_address"),
        ({"alert_url": "ftp://example.com/x"}, "invalid_alert_address"),
    ],
)
def test_a_watch_that_could_not_work_is_refused_when_it_is_saved(
    api: Api, body: dict[str, Any], code: str
) -> None:
    api.become_admin()
    response = api.post(BASE, _body(**body))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code
    assert api.get(BASE).json() == []


def test_the_number_of_watches_and_their_frequency_are_limited(api: Api) -> None:
    api.become_admin()
    assert (
        api.patch(
            "/api/v1/admin/settings", {"max_watches": 1, "min_watch_interval_s": 120}
        ).status_code
        == 200
    )
    assert api.post(BASE, _body(interval_s=60)).json()["error"]["code"] == "interval_too_short"
    assert api.post(BASE, _body(interval_s=120)).status_code == 201
    over = api.post(BASE, _body(name="Another", interval_s=120))
    assert over.status_code == 409 and over.json()["error"]["code"] == "watch_limit"


def test_a_webhook_on_a_private_address_can_be_allowed_by_the_administrator(api: Api) -> None:
    api.become_admin()
    body = _body(alert_url="http://10.0.0.5:8080/hook")
    assert api.post(BASE, body).status_code == 422
    assert api.patch("/api/v1/admin/settings", {"allow_private_webhooks": True}).status_code == 200
    assert api.post(BASE, body).status_code == 201


# ---------------------------------------------------------------------------------------------
# Running: the scheduler, a node, the judgement, the alert
# ---------------------------------------------------------------------------------------------


def _scheduler(api: Api) -> Any:
    return api.client.app.state.watches  # type: ignore[attr-defined]


def _wait(check: Callable[[], bool], seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("timed out waiting")


def _answer(session: Any, good: bool) -> dict[str, Any]:
    task = _task(session)
    if good:
        _send(session, task, type="sample", seq=0, status="ok", rtt_us=12_000)
    else:
        _send(session, task, type="sample", seq=0, status="timeout")
    _send(session, task, type="task_result", status="completed", tested_ip=task["target"].get("ip"))
    barrier(session)
    return task


def _watch(api: Api, watch_id: str) -> dict[str, Any]:
    return api.get(f"{BASE}/{watch_id}").json()  # type: ignore[no-any-return]


def test_a_due_watch_is_started_once_and_not_again_until_it_is_due(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=KINDS)
    try:
        watch_id = api.post(BASE, _body(count=1)).json()["id"]
        assert api.client.portal.call(_scheduler(api).tick) == 1
        _answer(session, good=True)
        _wait(lambda: _watch(api, watch_id)["last_status"] == "ok")
        assert api.client.portal.call(_scheduler(api).tick) == 0, "not due again for five minutes"
        run = _watch(api, watch_id)["history"][0]
        assert (run["status"], run["nodes_ok"], run["nodes_total"], run["median_ms"]) == (
            "ok",
            1,
            1,
            12.0,
        )
        # the measurement it made belongs to the watch, and the administrator can open it
        measurement = api.get(f"/api/v1/measurements/{run['measurement_id']}")
        assert measurement.status_code == 200 and measurement.json()["status"] == "completed"
    finally:
        ws.__exit__(None, None, None)


def test_two_instances_do_not_both_start_the_same_due_watch(api: Api) -> None:
    from ipping_backend.watches.service import WatchScheduler

    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=KINDS)
    try:
        api.post(BASE, _body(count=1))
        app = api.client.app
        other = WatchScheduler(app.state.sessionmaker, app.state.engine, resolver=fake_resolver)  # type: ignore[attr-defined]
        first = _scheduler(api)
        started = api.client.portal.call(lambda: asyncio.gather(first.tick(), other.tick()))
        assert sorted(started) == [0, 1], started
        _answer(session, good=True)
    finally:
        ws.__exit__(None, None, None)


def test_a_disabled_watch_does_not_run(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential, kinds=KINDS)
    try:
        api.post(BASE, _body(enabled=False))
        assert api.client.portal.call(_scheduler(api).tick) == 0
    finally:
        ws.__exit__(None, None, None)


def test_an_alert_is_sent_when_the_target_goes_bad_and_again_when_it_is_back(api: Api) -> None:
    sent: list[dict[str, Any]] = []

    def hook(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200)

    _scheduler(api)._transport = httpx.MockTransport(hook)  # noqa: SLF001
    _scheduler(api)._resolver = fake_resolver  # noqa: SLF001
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=KINDS)
    try:
        watch_id = api.post(BASE, _body(count=1, alert_url="https://example.com/hook")).json()["id"]

        def run(good: bool, expect_runs: int) -> None:
            assert api.post(f"{BASE}/{watch_id}/run").status_code == 202
            _answer(session, good)
            _wait(lambda: len(_watch(api, watch_id)["history"]) == expect_runs)

        run(False, 1)
        assert sent == [] and _watch(api, watch_id)["bad_streak"] == 1, (
            "one bad run is not an alert"
        )
        run(False, 2)
        _wait(lambda: len(sent) == 1)
        assert (
            sent[0]["event"] == "alert"
            and sent[0]["status"] == "down"
            and sent[0]["target"] == "example.com"
        )
        assert (
            _watch(api, watch_id)["alerting"] is True
            and _watch(api, watch_id)["last_alert_error"] is None
        )
        run(False, 3)
        assert len(sent) == 1, "alerted once, not on every bad run"
        run(True, 4)
        _wait(lambda: len(sent) == 2)
        assert sent[1]["event"] == "recovered"
        done = _watch(api, watch_id)
        assert done["alerting"] is False and done["bad_streak"] == 0 and done["last_status"] == "ok"
        assert [r["status"] for r in done["history"]] == ["down", "down", "down", "ok"]
    finally:
        ws.__exit__(None, None, None)


def test_an_alert_that_cannot_be_delivered_is_recorded_not_lost(api: Api) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _scheduler(api)._transport = httpx.MockTransport(fail)  # noqa: SLF001
    _scheduler(api)._resolver = fake_resolver  # noqa: SLF001
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=KINDS)
    try:
        watch_id = api.post(
            BASE, _body(count=1, alert_after=1, alert_url="https://example.com/hook")
        ).json()["id"]
        api.post(f"{BASE}/{watch_id}/run")
        _answer(session, good=False)
        _wait(lambda: _watch(api, watch_id)["last_alert_error"] is not None)
        done = _watch(api, watch_id)
        assert done["last_alert_error"] == "The server answered 500." and done["alerting"] is True
    finally:
        ws.__exit__(None, None, None)


def test_the_test_alert_reports_whether_it_arrived(api: Api) -> None:
    received: list[dict[str, Any]] = []

    def hook(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(204)

    _scheduler(api)._transport = httpx.MockTransport(hook)  # noqa: SLF001
    _scheduler(api)._resolver = fake_resolver  # noqa: SLF001
    api.become_admin()
    with_hook = api.post(BASE, _body(alert_url="https://example.com/hook")).json()["id"]
    assert api.post(f"{BASE}/{with_hook}/test-alert").json() == {"ok": True, "error": None}
    assert received[0]["event"] == "test"
    without = api.post(BASE, _body(name="Quiet")).json()["id"]
    assert api.post(f"{BASE}/{without}/test-alert").json()["error"]["code"] == "no_alert_address"


def test_a_check_that_cannot_start_is_an_error_run_and_never_an_alert(api: Api) -> None:
    sent: list[Any] = []
    _scheduler(api)._transport = httpx.MockTransport(
        lambda r: sent.append(r) or httpx.Response(200)
    )  # noqa: SLF001
    api.become_admin()
    # no node is online: the measurement cannot be made, which says nothing about the target
    watch_id = api.post(
        BASE, _body(count=1, alert_after=1, alert_url="https://example.com/hook")
    ).json()["id"]
    assert api.client.portal.call(_scheduler(api).tick) == 1
    done = _watch(api, watch_id)
    assert done["last_status"] == "error" and done["history"][0]["status"] == "error"
    assert "online node" in done["history"][0]["note"] and done["bad_streak"] == 0 and sent == []
    assert api.post(f"{BASE}/{watch_id}/run").json()["error"]["code"] == "could_not_start"


def test_old_runs_are_pruned_by_the_retention_sweep(api: Api) -> None:
    from datetime import timedelta

    from sqlalchemy import select

    from ipping_backend import maintenance
    from ipping_backend.models import Watch, WatchRun, utcnow

    api.become_admin()
    watch_id = api.post(BASE, _body()).json()["id"]
    app = api.client.app

    async def go() -> list[str]:
        async with app.state.sessionmaker() as db:  # type: ignore[attr-defined]
            db.add(WatchRun(watch_id=watch_id, at=utcnow() - timedelta(days=60), status="ok"))
            db.add(WatchRun(watch_id=watch_id, at=utcnow() - timedelta(days=1), status="down"))
            await db.commit()
        await maintenance.run_once(app.state.sessionmaker)  # type: ignore[attr-defined]
        async with app.state.sessionmaker() as db:  # type: ignore[attr-defined]
            assert await db.get(Watch, watch_id) is not None
            rows = (await db.execute(select(WatchRun.status))).scalars()
            return list(rows)

    assert api.client.portal.call(go) == ["down"]
