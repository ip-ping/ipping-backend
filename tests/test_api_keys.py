"""API keys: scripts that start measurements without a browser, within their own quotas."""

from __future__ import annotations

import json
import threading
from typing import Any

from ipping_backend.measurements.render import ms, render_text
from ipping_backend.measurements.schemas import SampleOut, Summary
from ipping_backend.nodes.protocol import DnsAnswerRecord, DnsDetail, HopDetail, HttpDetail

from .conftest import PASSWORD, Api
from .test_api import barrier, connect
from .test_probe_kinds import ALL_KINDS, _send, _task
from .test_watches import _measurement, _summary, _task_out

BASE = "/api/v1/admin/api-keys"


def _key(api: Api, name: str = "CI", **limits: int) -> dict[str, Any]:
    response = api.post(BASE, {"name": name, **limits})
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def _sign_in_again(api: Api) -> None:
    """The administrator comes back to the browser (the cookies were forgotten to act as a script)."""
    api.start_session()
    response = api.post("/api/v1/admin/login", {"username": "admin", "password": PASSWORD})
    assert response.status_code == 200, response.text
    api.csrf = response.json()["csrf_token"]


def _as_script(api: Api, key: str) -> dict[str, str]:
    """Forget the browser session: from here on only the key is presented."""
    api.client.cookies.clear()
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------------------------


def test_a_key_is_shown_once_and_only_its_hash_is_kept(api: Api) -> None:
    api.become_admin()
    created = _key(api, per_minute=5)
    assert created["key"].startswith("ipk_") and len(created["key"]) > 40
    assert created["prefix"] == created["key"][:12] and created["per_minute"] == 5
    listed = api.get(BASE).json()
    assert [k["name"] for k in listed] == ["CI"] and "key" not in listed[0]
    assert created["key"] not in json.dumps(listed) and "key_hash" not in listed[0]
    audit = json.dumps(api.get("/api/v1/admin/audit").json())
    assert "api_key.create" in audit and created["key"] not in audit


def test_only_an_administrator_manages_keys(api: Api) -> None:
    api.start_session()
    assert api.get(BASE).status_code == 401 and api.post(BASE, {"name": "x"}).status_code == 401


def test_a_key_can_be_renamed_given_other_quotas_and_revoked(api: Api) -> None:
    api.become_admin()
    created = _key(api)
    changed = api.patch(f"{BASE}/{created['id']}", {"name": "Deploys", "per_minute": 3}).json()
    assert (changed["name"], changed["per_minute"], changed["per_day"]) == ("Deploys", 3, 1000)
    assert api.request("DELETE", f"{BASE}/{created['id']}").status_code == 204
    assert (
        api.request("DELETE", f"{BASE}/{created['id']}").status_code == 204
    )  # revoking twice is fine
    assert api.get(BASE).json()[0]["revoked_at"] is not None
    assert api.request("DELETE", f"{BASE}/nope").status_code == 404


# ---------------------------------------------------------------------------------------------
# Using one
# ---------------------------------------------------------------------------------------------


def test_a_key_starts_and_reads_its_own_measurements_without_a_session(api: Api) -> None:
    api.become_admin()
    key = _key(api)["key"]
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=ALL_KINDS)
    try:
        headers = _as_script(api, key)
        # no session and no key: refused, as before
        assert (
            api.client.post("/api/v1/measurements", json={"target": "example.com"}).status_code
            == 401
        )
        created = api.client.post(
            "/api/v1/measurements", json={"target": "example.com", "count": 1}, headers=headers
        )
        assert created.status_code == 201, created.text
        measurement_id = created.json()["id"]
        task = _task(session)
        _send(session, task, type="sample", seq=0, status="ok", rtt_us=9_000)
        _send(session, task, type="task_result", status="completed", tested_ip=task["target"]["ip"])
        barrier(session)

        read = api.client.get(f"/api/v1/measurements/{measurement_id}", headers=headers)
        assert read.status_code == 200 and read.json()["status"] == "completed"
        assert api.client.get(f"/api/v1/measurements/{measurement_id}").status_code == 404
        mine = api.client.get("/api/v1/measurements", headers=headers).json()
        assert [m["id"] for m in mine] == [measurement_id]
        location = api.client.get(
            f"/api/v1/measurements/{measurement_id}/location", headers=headers
        )
        assert location.status_code == 200
    finally:
        ws.__exit__(None, None, None)


def test_another_key_cannot_see_it_but_an_administrator_can(api: Api) -> None:
    api.become_admin()
    first, second = _key(api, "first")["key"], _key(api, "second")["key"]
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential, kinds=ALL_KINDS)
    try:
        headers = _as_script(api, first)
        measurement_id = api.client.post(
            "/api/v1/measurements", json={"target": "example.com", "count": 1}, headers=headers
        ).json()["id"]
        other = api.client.get(
            f"/api/v1/measurements/{measurement_id}", headers={"Authorization": f"Bearer {second}"}
        )
        assert other.status_code == 404
        cancel = api.client.post(
            f"/api/v1/measurements/{measurement_id}/cancel", headers={"X-API-Key": second}
        )
        assert cancel.status_code == 404
        # its owner may cancel it, and so may an administrator who looks
        assert (
            api.client.post(
                f"/api/v1/measurements/{measurement_id}/cancel", headers=headers
            ).status_code
            == 202
        )
        _sign_in_again(api)
        assert api.get(f"/api/v1/measurements/{measurement_id}").status_code == 200
    finally:
        ws.__exit__(None, None, None)


def test_a_wrong_or_revoked_key_is_refused_not_ignored(api: Api) -> None:
    api.become_admin()
    created = _key(api)
    for bad in ("ipk_not-a-real-key", created["key"] + "x"):
        response = api.client.get(
            "/api/v1/measurements", headers={"Authorization": f"Bearer {bad}"}
        )
        assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_api_key"
    api.request("DELETE", f"{BASE}/{created['id']}")
    headers = _as_script(api, created["key"])
    revoked = api.client.post(
        "/api/v1/measurements", json={"target": "example.com"}, headers=headers
    )
    assert revoked.status_code == 401 and revoked.json()["error"]["code"] == "invalid_api_key"
    # something that is not shaped like a key is not a key, so the request is simply anonymous
    odd = api.client.get("/api/v1/measurements", headers={"Authorization": "Bearer something-else"})
    assert odd.status_code == 401 and odd.json()["error"]["code"] == "session_required"


def test_a_key_has_its_own_quotas(api: Api) -> None:
    api.become_admin()
    small = _key(api, "small", per_minute=2)["key"]
    day = _key(api, "day", per_day=1)["key"]
    big = _key(api, "big")["key"]
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential, kinds=ALL_KINDS)
    try:
        api.patch("/api/v1/admin/settings", {"max_concurrent_per_session": 50})
        api.client.cookies.clear()

        def start(key: str) -> Any:
            return api.client.post(
                "/api/v1/measurements",
                json={"target": "example.com", "count": 1},
                headers={"Authorization": f"Bearer {key}"},
            )

        assert start(small).status_code == 201 and start(small).status_code == 201
        third = start(small)
        assert third.status_code == 429 and third.json()["error"]["code"] == "rate_limited"
        assert "2 measurements a minute" in third.json()["error"]["message"]
        assert start(day).status_code == 201
        assert "a day" in start(day).json()["error"]["message"]
        assert start(big).status_code == 201, "another key's budget is its own"
        _sign_in_again(api)
        listed = {k["name"]: k["made_24h"] for k in api.get(BASE).json()}
        assert listed == {"small": 2, "day": 1, "big": 1}
    finally:
        ws.__exit__(None, None, None)


def test_a_key_cannot_run_more_measurements_at_once_than_a_visitor_can(api: Api) -> None:
    api.become_admin()
    key = _key(api)["key"]
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential, kinds=ALL_KINDS)
    try:
        headers = _as_script(api, key)
        statuses = [
            api.client.post(
                "/api/v1/measurements", json={"target": "example.com", "count": 1}, headers=headers
            ).status_code
            for _ in range(3)
        ]
        assert statuses == [201, 201, 429]  # max_concurrent_per_session defaults to 2
    finally:
        ws.__exit__(None, None, None)


def test_last_use_is_recorded(api: Api) -> None:
    api.become_admin()
    created = _key(api)
    assert created["last_used_at"] is None
    headers = _as_script(api, created["key"])
    assert api.client.get("/api/v1/measurements", headers=headers).status_code == 200
    _sign_in_again(api)
    assert api.get(BASE).json()[0]["last_used_at"] is not None


# ---------------------------------------------------------------------------------------------
# The text endpoint
# ---------------------------------------------------------------------------------------------


def test_the_text_endpoint_needs_a_key_not_a_session(api: Api) -> None:
    api.become_admin()
    assert api.get("/api/v1/text?target=example.com").json()["error"]["code"] == "api_key_required"
    key = _key(api)["key"]
    bad = api.client.get("/api/v1/text?target=example.com&kind=scan", headers=_as_script(api, key))
    assert bad.status_code == 422


def test_the_text_endpoint_waits_for_the_answer_and_prints_it(api: Api) -> None:
    api.become_admin()
    key = _key(api)["key"]
    _, credential = api.enroll_node("Dubai")
    ws, session = connect(api, credential, kinds=ALL_KINDS)
    try:
        headers = _as_script(api, key)
        result: dict[str, Any] = {}

        def call() -> None:
            result["response"] = api.client.get(
                "/api/v1/text?target=example.com&count=2&wait=10", headers=headers
            )

        thread = threading.Thread(target=call)
        thread.start()
        task = _task(session)
        for seq, rtt in enumerate((12_000, 14_000)):
            _send(session, task, type="sample", seq=seq, status="ok", rtt_us=rtt)
        _send(session, task, type="task_result", status="completed", tested_ip=task["target"]["ip"])
        barrier(session)
        thread.join(15)
        response = result["response"]
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/plain")
        assert (
            response.headers["x-measurement-status"] == "completed"
            and response.headers["x-measurement-id"]
        )
        assert "Ping example.com" in response.text and "2 echo requests per node" in response.text
        assert "2/2 received  min/median/avg/max = 12.0/13.0/13.0/14.0 ms  loss 0%" in response.text
        assert "1 of 1 nodes answered. Measured with ipping." in response.text
    finally:
        ws.__exit__(None, None, None)


def test_the_text_endpoint_says_when_it_is_still_waiting(api: Api) -> None:
    api.become_admin()
    key = _key(api)["key"]
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential, kinds=ALL_KINDS)
    try:
        response = api.client.get(
            "/api/v1/text?target=example.com&wait=1", headers=_as_script(api, key)
        )
        assert response.status_code == 200 and response.headers["x-measurement-status"] == "running"
        assert "still running" in response.text
    finally:
        ws.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------
# The report itself
# ---------------------------------------------------------------------------------------------


def test_numbers_are_printed_like_the_interface_does() -> None:
    assert [ms(v) for v in (None, 1.44, 12.55, 99.99, 100.4, 1234.6)] == [
        "–",
        "1.4",
        "12.6",
        "100.0",
        "100",
        "1235",
    ]


def test_a_ping_report_lists_each_node() -> None:
    good = _task_out(_summary(4, min_ms=1.4, median_ms=3.1, avg_ms=3.1, max_ms=4.7))
    gone = _task_out(Summary(sent=4, received=0, loss_pct=100.0))
    text = render_text(_measurement("ping", [good, gone]))
    assert "Ping example.com (2026-11-07 12:00 UTC, 4 echo requests per node)" in text
    assert (
        "N, ?" in text and "4/4 received  min/median/avg/max = 1.4/3.1/3.1/4.7 ms  loss 0%" in text
    )
    assert "0/4 received" in text


def test_http_dns_and_route_reports() -> None:
    http = HttpDetail(
        status_code=200,
        dns_us=1000,
        connect_us=20_000,
        tls_us=40_000,
        ttfb_us=30_000,
        total_us=91_000,
    )
    web = _task_out(samples=[SampleOut(seq=0, status="ok", http=http)])
    assert "200  91.0 ms  (dns 1.0, connect 20.0, tls 40.0, first byte 30.0)" in render_text(
        _measurement("http", [web])
    )
    refused = _task_out(samples=[SampleOut(seq=0, status="timeout")])
    assert "timed out" in render_text(_measurement("http", [refused]))

    answer = DnsAnswerRecord(name="e.com", type="MX", ttl=60, data="10 mail.example.com")
    dns = DnsDetail(resolver="system", record_type="MX", rcode="NOERROR", answers=[answer])
    named = _task_out(samples=[SampleOut(seq=0, status="ok", rtt_ms=12.0, dns=dns)])
    out = render_text(_measurement("dns", [named]))
    assert "10 mail.example.com  12.0 ms" in out and "DNS example.com" in out
    nx = DnsDetail(resolver="system", record_type="A", rcode="NXDOMAIN")
    gone = _task_out(samples=[SampleOut(seq=0, status="ok", rtt_ms=5.0, dns=nx)])
    assert "no such name" in render_text(_measurement("dns", [gone]))

    hops = [
        SampleOut(seq=0, status="ok", hop=HopDetail(ttl=1, private=True, rtts_us=[500])),
        SampleOut(seq=1, status="timeout", hop=HopDetail(ttl=2)),
        SampleOut(
            seq=2,
            status="ok",
            hop=HopDetail(ttl=3, ip="93.184.216.34", reached=True, rtts_us=[11_000]),
        ),
    ]
    route = _task_out(_summary(2, reached=True, median_ms=11.0), samples=hops)
    text = render_text(_measurement("traceroute", [route]))
    assert "reached in 3 hops, 11.0 ms" in text and "(private network)" in text and "  2  *" in text
