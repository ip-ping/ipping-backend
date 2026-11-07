"""tcp, http and traceroute measurements: what is asked of a node and what is kept of its answer."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ipping_backend.measurements.plan import plan_measurement, probe_cost, sample_limit
from ipping_backend.measurements.schemas import CreateMeasurementRequest, SampleOut, summarize
from ipping_backend.nodes import protocol
from ipping_backend.runtime_settings import RuntimeSettings
from ipping_backend.security.targets import parse_target

from .conftest import Api
from .test_api import barrier, connect

ALL_KINDS = ("ping", "tcp", "http", "traceroute")


def _task(session: Any) -> dict[str, Any]:
    task = json.loads(session.receive_text())
    assert task["type"] == "task"
    session.send_text(
        json.dumps(
            {"type": "task_ack", "task_id": task["task_id"], "attempt_id": task["attempt_id"]}
        )
    )
    return task  # type: ignore[no-any-return]


def _send(session: Any, task: dict[str, Any], **frame: Any) -> None:
    ids = {"task_id": task["task_id"], "attempt_id": task["attempt_id"]}
    session.send_text(json.dumps({**ids, **frame}))


def _complete(session: Any, task: dict[str, Any]) -> None:
    _send(session, task, type="task_result", status="completed", tested_ip=task["target"].get("ip"))
    barrier(session)


def _start(api: Api, **body: Any) -> dict[str, Any]:
    response = api.post("/api/v1/measurements", body)
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------------------------
# Planning and parsing: no node needed
# ---------------------------------------------------------------------------------------------


def _plan(kind: str, target: str, **extra: Any) -> Any:
    request = CreateMeasurementRequest(target=target, kind=kind, **extra)  # type: ignore[arg-type]
    return plan_measurement(request, parse_target(target), RuntimeSettings())


def test_parse_keeps_port_scheme_and_path_but_never_the_query() -> None:
    parsed = parse_target("https://user:pw@example.com:8443/a%20b/c?token=secret#frag")
    assert (parsed.scheme, parsed.port, parsed.path) == ("https", 8443, "/a%20b/c")
    assert parse_target("example.com:25").port == 25
    assert parse_target("[2001:4860:4860::8888]:443").port == 443
    assert parse_target("example.com").port is None
    assert parse_target("example.com/").path == ""


def test_each_kind_has_its_own_options_and_cost() -> None:
    ping = _plan("ping", "example.com", count=3)
    assert ping.options == {"count": 3, "interval_ms": 1000, "timeout_ms": 2000}

    tcp = _plan("tcp", "example.com:443")
    assert tcp.port == 443 and tcp.options["port"] == 443 and tcp.options["timeout_ms"] == 3000

    http = _plan("http", "http://example.com/health")
    assert (http.port, http.options["scheme"], http.options["path"]) == (80, "http", "/health")
    assert http.options["max_redirects"] == 0
    assert _plan("http", "example.com", follow_redirects=True).options["max_redirects"] == 5
    assert _plan("http", "example.com").options["scheme"] == "https"
    assert _plan("http", "example.com:8080").options["scheme"] == "http"

    route = _plan("traceroute", "example.com")
    assert route.options["max_hops"] == 30 and route.deadline_s >= 45

    assert sample_limit("ping", ping.options) == 3
    assert sample_limit("http", http.options) == 1
    assert sample_limit("traceroute", route.options) == 30
    assert probe_cost("tcp", tcp.options) == 4
    assert probe_cost("http", http.options) == 2
    assert probe_cost("traceroute", route.options) == 30
    assert route.options["count"] == 1


@pytest.mark.parametrize(
    ("kind", "target", "extra", "code"),
    [
        ("tcp", "example.com", {}, "port_required"),
        ("tcp", "example.com:6379", {}, "port_not_allowed"),
        ("tcp", "example.com", {"port": 23}, "port_not_allowed"),
        ("http", "https://example.com:9200/", {}, "port_not_allowed"),
        ("http", "example.com", {"count": 3}, "invalid_option"),
        ("ping", "example.com", {"follow_redirects": True}, "invalid_option"),
        ("ping", "example.com", {"port": 80}, "invalid_option"),
        ("traceroute", "example.com", {"port": 80}, "invalid_option"),
    ],
)
def test_plans_that_make_no_sense_are_refused(
    kind: str, target: str, extra: dict[str, Any], code: str
) -> None:
    from ipping_backend.measurements.plan import PlanError

    with pytest.raises(PlanError) as caught:
        _plan(kind, target, **extra)
    assert caught.value.code == code


def test_a_traceroute_can_be_repeated_like_mtr() -> None:
    from ipping_backend.measurements.plan import PlanError

    once = _plan("traceroute", "example.com")
    five = _plan("traceroute", "example.com", count=5)
    assert five.options["count"] == 5 and five.options["interval_ms"] == 1000
    assert five.options["max_hops"] == once.options["max_hops"]
    # A hop still produces one sample, but four more rounds take about 4 x (interval + timeout).
    assert sample_limit("traceroute", five.options) == 30
    assert five.deadline_s >= once.deadline_s and five.deadline_s >= 30 + 4 * 2 + 15
    # Every extra round adds one packet per hop, a third of what the first round counted for.
    assert probe_cost("traceroute", once.options) == 30
    assert probe_cost("traceroute", five.options) == 70
    assert probe_cost("traceroute", _plan("traceroute", "example.com", count=2).options) == 40

    request = CreateMeasurementRequest(target="example.com", kind="traceroute", count=6)
    with pytest.raises(PlanError) as caught:
        plan_measurement(request, parse_target("example.com"), RuntimeSettings(max_count=4))
    assert caught.value.code == "count_too_high"


def test_disabled_kinds_are_refused() -> None:
    from ipping_backend.measurements.plan import PlanError

    request = CreateMeasurementRequest(target="example.com", kind="traceroute")
    settings = RuntimeSettings(enabled_kinds=["ping"])
    with pytest.raises(PlanError) as caught:
        plan_measurement(request, parse_target("example.com"), settings)
    assert caught.value.code == "kind_disabled"


def test_tasks_need_what_their_kind_needs() -> None:
    base = {
        "task_id": "t",
        "attempt_id": "a",
        "target": {"ip": "93.184.216.34"},
        "expires_at": "2026-11-06T12:00:30Z",
    }
    with pytest.raises(ValueError, match="needs a port"):
        protocol.TaskFrame.model_validate({**base, "kind": "tcp", "options": {"timeout_ms": 2000}})
    with pytest.raises(ValueError, match="scheme and a path"):
        protocol.TaskFrame.model_validate(
            {
                **base,
                "kind": "http",
                "target": {**base["target"], "port": 443},
                "options": {"timeout_ms": 2000},
            }  # type: ignore[dict-item]
        )
    with pytest.raises(ValueError, match="max_hops"):
        protocol.TaskFrame.model_validate(
            {**base, "kind": "traceroute", "options": {"timeout_ms": 1000}}
        )
    with pytest.raises(ValueError, match="Input should"):
        protocol.TaskFrame.model_validate({**base, "kind": "scan", "options": {"timeout_ms": 1000}})


def test_oversized_detail_is_dropped_but_the_sample_is_kept() -> None:
    frame = protocol.node_to_backend.validate_python(
        {
            "type": "sample",
            "task_id": "t",
            "attempt_id": "a",
            "seq": 0,
            "status": "ok",
            "rtt_us": 5,
            "detail": {"server": "x" * 9000},
        }
    )
    assert isinstance(frame, protocol.SampleFrame) and frame.detail is None and frame.rtt_us == 5


def test_hop_and_http_details_are_bounded_and_cleaned() -> None:
    assert protocol.parse_detail("http", {"status_code": 999}) is None
    assert protocol.parse_detail("traceroute", {"ttl": 0}) is None
    assert protocol.parse_detail("ping", {"status_code": 200}) is None
    detail = protocol.parse_detail("http", {"status_code": 200, "server": "ngi\x1b[31mnx\n"})
    assert isinstance(detail, protocol.HttpDetail) and detail.server == "ngi?[31mnx?"


def test_a_repeated_hop_says_how_many_probes_it_got() -> None:
    parsed = protocol.parse_detail(
        "traceroute",
        {"ttl": 3, "ip": "62.115.1.1", "rtts_us": [1000, 2000, 3000, 4000], "sent": 6},
    )
    assert isinstance(parsed, protocol.HopDetail) and parsed.sent == 6
    # An older node sends no count: the hop is a plain one-round hop.
    plain = protocol.parse_detail("traceroute", {"ttl": 3, "rtts_us": [1000]})
    assert isinstance(plain, protocol.HopDetail) and plain.sent is None
    # More answers than probes, no probes at all, or an absurd number of replies: dropped.
    assert protocol.parse_detail("traceroute", {"ttl": 3, "rtts_us": [1, 2, 3], "sent": 2}) is None
    assert protocol.parse_detail("traceroute", {"ttl": 3, "rtts_us": [], "sent": 0}) is None
    assert protocol.parse_detail("traceroute", {"ttl": 3, "rtts_us": [1] * 33}) is None


def test_route_summary_is_about_reaching_the_destination() -> None:
    def hop(ttl: int, status: str, rtts: list[int], reached: bool = False) -> SampleOut:
        return SampleOut(
            seq=ttl - 1,
            status=status,  # type: ignore[arg-type]
            rtt_ms=min(rtts) / 1000 if rtts else None,
            hop=protocol.HopDetail(ttl=ttl, rtts_us=rtts, reached=reached),
        )

    arrived = summarize(
        [hop(1, "ok", [1000]), hop(2, "timeout", []), hop(3, "ok", [9000, 11000], True)],
        "traceroute",
    )
    assert arrived is not None
    assert (arrived.sent, arrived.received, arrived.reached) == (3, 2, True)
    assert arrived.min_ms == 9.0 and arrived.max_ms == 11.0

    lost = summarize([hop(1, "ok", [1000]), hop(2, "timeout", [])], "traceroute")
    assert lost is not None and lost.reached is False and lost.median_ms is None


# ---------------------------------------------------------------------------------------------
# Through a node
# ---------------------------------------------------------------------------------------------


def test_tcp_measurement_counts_refusals_as_answers_from_the_host_but_not_as_connections(
    api: Api,
) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=ALL_KINDS)
    try:
        measurement = _start(api, target="example.com:443", kind="tcp", count=4)
        assert measurement["kind"] == "tcp" and measurement["options"]["port"] == 443
        task = _task(session)
        assert task["kind"] == "tcp"
        assert task["target"] == {"ip": "93.184.216.34", "family": "auto", "port": 443}
        assert task["options"] == {"count": 4, "interval_ms": 1000, "timeout_ms": 3000}
        _send(session, task, type="sample", seq=0, status="ok", rtt_us=12_000)
        _send(
            session,
            task,
            type="sample",
            seq=1,
            status="error",
            rtt_us=11_000,
            error_code="connection_refused",
        )
        _send(
            session, task, type="sample", seq=2, status="timeout", rtt_us=99, error_code="timeout"
        )
        _send(session, task, type="sample", seq=3, status="ok", rtt_us=14_000)
        _complete(session, task)

        done = api.get(f"/api/v1/measurements/{measurement['id']}").json()["tasks"][0]
        assert done["summary"]["sent"] == 4 and done["summary"]["received"] == 2
        assert done["summary"]["median_ms"] == 13.0
        refused = done["samples"][1]
        assert refused["status"] == "error" and refused["rtt_ms"] == 11.0
        assert done["samples"][2]["rtt_ms"] is None  # a timeout never carries a time
    finally:
        ws.__exit__(None, None, None)


def test_http_measurement_asks_for_the_name_and_keeps_only_validated_detail(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=ALL_KINDS)
    try:
        measurement = _start(
            api,
            target="https://user:pw@example.com/a/b?token=secret",
            kind="http",
            follow_redirects=True,
        )
        assert "secret" not in json.dumps(measurement) and "pw" not in json.dumps(measurement)
        assert (
            measurement["options"]["path"] == "/a/b"
            and measurement["options"]["max_redirects"] == 5
        )
        task = _task(session)
        assert task["kind"] == "http"
        assert task["target"] == {
            "ip": "93.184.216.34",
            "family": "auto",
            "port": 443,
            "server_name": "example.com",
        }
        assert task["options"]["scheme"] == "https" and task["options"]["path"] == "/a/b"
        detail = {
            "status_code": 301,
            "http_version": "HTTP/1.1",
            "server": "nginx",
            "dns_us": 0,
            "connect_us": 8000,
            "tls_us": 21_000,
            "ttfb_us": 40_000,
            "total_us": 69_000,
            "final_scheme": "https",
            "final_host": "example.com",
            "final_path": "/a/b",
            "tls": {
                "version": "TLSv1.3",
                "subject": "CN=example.com",
                "issuer": "CN=Test CA",
                "not_after": "2027-01-01T00:00:00Z",
                "verified": True,
                "surprise": "ignored",
            },
            "redirects": [
                {
                    "status_code": 301,
                    "location_scheme": "https",
                    "location_host": "www.example.com",
                    "followed": False,
                }
            ],
            "unexpected": "ignored",
        }
        _send(session, task, type="sample", seq=0, status="ok", rtt_us=69_000, detail=detail)
        _send(
            session, task, type="sample", seq=1, status="ok", rtt_us=1
        )  # a second request: ignored
        _complete(session, task)

        done = api.get(f"/api/v1/measurements/{measurement['id']}").json()["tasks"][0]
        assert len(done["samples"]) == 1
        http = done["samples"][0]["http"]
        assert http["status_code"] == 301 and http["tls"]["verified"] is True
        assert http["tls"]["not_after"].startswith("2027-01-01")
        assert http["redirects"][0]["location_host"] == "www.example.com"
        assert "unexpected" not in http and "surprise" not in http["tls"]
        assert done["summary"] == {
            "sent": 1, "received": 1, "loss_pct": 0.0, "min_ms": 69.0, "median_ms": 69.0,
            "avg_ms": 69.0, "max_ms": 69.0, "reached": None,
        }  # fmt: skip
    finally:
        ws.__exit__(None, None, None)


def test_http_to_an_address_presents_no_name(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=ALL_KINDS)
    try:
        _start(api, target="http://93.184.216.34/", kind="http")
        task = _task(session)
        assert task["target"] == {"ip": "93.184.216.34", "family": "auto", "port": 80}
        assert task["options"]["max_redirects"] == 0
    finally:
        ws.__exit__(None, None, None)


def test_invalid_http_detail_drops_the_detail_not_the_sample(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=ALL_KINDS)
    try:
        measurement = _start(api, target="example.com", kind="http")
        task = _task(session)
        _send(
            session,
            task,
            type="sample",
            seq=0,
            status="ok",
            rtt_us=5000,
            detail={"status_code": 12},
        )
        _complete(session, task)
        sample = api.get(f"/api/v1/measurements/{measurement['id']}").json()["tasks"][0]["samples"][
            0
        ]
        assert sample["rtt_ms"] == 5.0 and sample["http"] is None
    finally:
        ws.__exit__(None, None, None)


def test_traceroute_never_discloses_a_non_public_hop(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=ALL_KINDS)
    try:
        measurement = _start(api, target="example.com", kind="traceroute")
        task = _task(session)
        assert task["kind"] == "traceroute" and task["options"]["max_hops"] == 30
        hops = [
            ("ok", {"ttl": 1, "ip": "192.168.1.1", "rtts_us": [800]}),
            ("ok", {"ttl": 2, "ip": "100.64.3.4", "rtts_us": [2000, 2100]}),
            ("timeout", {"ttl": 3, "rtts_us": []}),
            ("ok", {"ttl": 4, "ip": "203.0.113.9", "rtts_us": [9000]}),
            ("ok", {"ttl": 5, "ip": "93.184.216.34", "rtts_us": [12_000, 11_000, 13_000], "reached": True}),
        ]  # fmt: skip
        for seq, (status, detail) in enumerate(hops):
            rtt = min(detail["rtts_us"]) if detail["rtts_us"] else None  # type: ignore[type-var]
            _send(session, task, type="sample", seq=seq, status=status, rtt_us=rtt, detail=detail)
        _complete(session, task)

        final = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        done = final["tasks"][0]
        first, second, silent, third, last = (s["hop"] for s in done["samples"])
        assert first == {
            "ttl": 1,
            "ip": None,
            "private": True,
            "rtts_us": [800],
            "reached": False,
            "sent": None,
        }
        assert second["ip"] is None and second["private"] is True  # carrier-grade NAT
        assert silent is None or silent["private"] is False
        assert (
            third["ip"] == "203.0.113.9" or third["ip"] is None
        )  # documentation range: not public
        assert last["ip"] == "93.184.216.34" and last["reached"] is True
        assert done["summary"]["reached"] is True and done["summary"]["median_ms"] == 12.0
        assert final["stats"]["nodes_responding"] == 1
    finally:
        ws.__exit__(None, None, None)


def test_a_repeated_traceroute_is_asked_for_rounds_and_keeps_every_reply(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=ALL_KINDS)
    try:
        measurement = _start(api, target="example.com", kind="traceroute", count=4)
        assert measurement["options"]["count"] == 4
        task = _task(session)
        assert task["options"]["count"] == 4 and task["options"]["interval_ms"] == 1000
        hops = [
            {"ttl": 1, "ip": "203.0.113.9", "rtts_us": [900, 1000, 1100, 1200, 800, 950], "sent": 6},
            {"ttl": 2, "ip": "62.115.1.1", "rtts_us": [9000, 9500, 12000], "sent": 6},
            {"ttl": 3, "ip": "93.184.216.34", "rtts_us": [20_000] * 6, "sent": 6, "reached": True},
        ]  # fmt: skip
        for seq, detail in enumerate(hops):
            _send(
                session, task, type="sample", seq=seq, status="ok",
                rtt_us=min(detail["rtts_us"]), detail=detail,
            )  # fmt: skip
        _complete(session, task)

        done = api.get(f"/api/v1/measurements/{measurement['id']}").json()["tasks"][0]
        second = done["samples"][1]["hop"]
        assert second["sent"] == 6 and second["rtts_us"] == [9000, 9500, 12000]
        assert done["summary"]["reached"] is True

        # The plain-text view shows loss and spread instead of every reply.
        from ipping_backend.measurements.render import render_text
        from ipping_backend.measurements.schemas import MeasurementOut

        text = render_text(
            MeasurementOut.model_validate(
                api.get(f"/api/v1/measurements/{measurement['id']}").json()
            )
        )
        assert "50% loss" in text and "best/avg/worst = 9.0/10.2/12.0 ms" in text, text
        assert "0% loss" in text
    finally:
        ws.__exit__(None, None, None)


def test_a_traceroute_that_never_arrives_is_not_a_responding_node(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=ALL_KINDS)
    try:
        measurement = _start(api, target="example.com", kind="traceroute")
        task = _task(session)
        _send(
            session,
            task,
            type="sample",
            seq=0,
            status="ok",
            rtt_us=900,
            detail={"ttl": 1, "ip": "8.8.4.4", "rtts_us": [900]},
        )
        _send(
            session, task, type="sample", seq=1, status="timeout", detail={"ttl": 2, "rtts_us": []}
        )
        _complete(session, task)
        final = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        assert final["tasks"][0]["summary"]["reached"] is False
        assert final["stats"]["nodes_responding"] == 0
    finally:
        ws.__exit__(None, None, None)


def test_only_nodes_that_advertise_a_kind_get_it(api: Api) -> None:
    api.become_admin()
    _, old_credential = api.enroll_node("Old node")
    ws, _ = connect(api, old_credential)  # a v0.2 node: ping only
    try:
        refused = api.post("/api/v1/measurements", {"target": "example.com:443", "kind": "tcp"})
        assert (
            refused.status_code == 409 and refused.json()["error"]["code"] == "kind_not_supported"
        )
        assert "updated" in refused.json()["error"]["message"]
        assert api.post("/api/v1/measurements", {"target": "example.com"}).status_code == 201
    finally:
        ws.__exit__(None, None, None)


def test_the_node_list_says_which_kinds_each_node_can_run(api: Api) -> None:
    api.become_admin()
    _, old = api.enroll_node("Old")
    _, new = api.enroll_node("New")
    ws_old, _ = connect(api, old)
    ws_new, _ = connect(api, new, kinds=ALL_KINDS)
    try:
        kinds = {n["name"]: n["kinds"] for n in api.get("/api/v1/nodes").json()}
        assert kinds == {"Old": ["ping"], "New": list(ALL_KINDS)}
        admin = {n["name"]: n["kinds"] for n in api.get("/api/v1/admin/nodes").json()}
        assert admin == kinds
    finally:
        ws_new.__exit__(None, None, None)
        ws_old.__exit__(None, None, None)


def test_kinds_and_ports_are_administered(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential, kinds=ALL_KINDS)
    try:
        settings = api.get("/api/v1/admin/settings").json()
        assert set(settings["enabled_kinds"]) == {*ALL_KINDS, "dns"}
        assert 443 in settings["allowed_ports"]

        bad = api.patch("/api/v1/admin/settings", {"enabled_kinds": ["ping", "scan"]})
        assert bad.status_code == 422
        assert api.patch("/api/v1/admin/settings", {"allowed_ports": [70000]}).status_code == 422

        api.patch(
            "/api/v1/admin/settings",
            {"enabled_kinds": ["ping", "tcp"], "allowed_ports": [9999, 9999, 443]},
        )
        assert api.get("/api/v1/admin/settings").json()["allowed_ports"] == [443, 9999]
        off = api.post("/api/v1/measurements", {"target": "example.com", "kind": "traceroute"})
        assert off.status_code == 422 and off.json()["error"]["code"] == "kind_disabled"
        assert (
            api.post(
                "/api/v1/measurements", {"target": "example.com:9999", "kind": "tcp"}
            ).status_code
            == 201
        )
    finally:
        ws.__exit__(None, None, None)


def test_location_is_estimated_from_round_trips_only(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=ALL_KINDS)
    try:
        measurement = _start(api, target="example.com", kind="http")
        task = _task(session)
        _send(session, task, type="sample", seq=0, status="ok", rtt_us=5000)
        _complete(session, task)
        location = api.get(f"/api/v1/measurements/{measurement['id']}/location").json()
        assert location["status"] == "unavailable" and "ping or TCP" in location["summary"]
    finally:
        ws.__exit__(None, None, None)
