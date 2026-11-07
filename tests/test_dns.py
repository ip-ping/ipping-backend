"""dns measurements: what is asked of a node, who may be asked, and what is kept of the answer."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ipping_backend.measurements.plan import (
    DNS_RESOLVERS,
    PlanError,
    plan_measurement,
    probe_cost,
    sample_limit,
)
from ipping_backend.measurements.schemas import CreateMeasurementRequest
from ipping_backend.nodes import protocol
from ipping_backend.runtime_settings import RuntimeSettings
from ipping_backend.security.targets import address_is_public, parse_target

from .conftest import Api
from .test_api import barrier, connect
from .test_probe_kinds import ALL_KINDS, _send, _start, _task

KINDS_WITH_DNS = (*ALL_KINDS, "dns")


def _plan(target: str, **extra: Any) -> Any:
    request = CreateMeasurementRequest(target=target, kind="dns", **extra)
    return plan_measurement(request, parse_target(target), RuntimeSettings())


# ---------------------------------------------------------------------------------------------
# Planning: no node needed
# ---------------------------------------------------------------------------------------------


def test_a_dns_check_asks_one_question_with_the_nodes_own_resolver_by_default() -> None:
    plan = _plan("example.com")
    assert plan.options == {
        "count": 1,
        "interval_ms": 1000,
        "timeout_ms": 5000,
        "record_type": "A",
        "resolver": "system",
    }
    assert plan.port is None
    assert sample_limit("dns", plan.options) == 1
    assert probe_cost("dns", plan.options) == 1


@pytest.mark.parametrize(("name", "address"), sorted(DNS_RESOLVERS.items()))
def test_a_named_resolver_becomes_its_public_address(name: str, address: str) -> None:
    plan = _plan("example.com", resolver=name, record_type="MX")
    assert plan.options["resolver"] == name
    assert plan.options["resolver_ip"] == address
    assert plan.options["record_type"] == "MX"
    assert address_is_public(__import__("ipaddress").ip_address(address))


def test_the_url_a_person_pastes_is_reduced_to_its_name() -> None:
    assert parse_target("https://user:pw@www.example.com/a?b=c").host == "www.example.com"


@pytest.mark.parametrize(
    ("target", "extra", "code"),
    [
        ("93.184.216.34", {}, "invalid_target"),
        ("example.com", {"count": 3}, "invalid_option"),
        ("example.com", {"port": 53}, "invalid_option"),
        ("example.com", {"follow_redirects": True}, "invalid_option"),
    ],
)
def test_dns_plans_that_make_no_sense_are_refused(
    target: str, extra: dict[str, Any], code: str
) -> None:
    with pytest.raises(PlanError) as caught:
        _plan(target, **extra)
    assert caught.value.code == code


@pytest.mark.parametrize("kind", ["ping", "tcp", "http", "traceroute"])
def test_only_a_dns_check_takes_a_record_type_or_a_resolver(kind: str) -> None:
    for extra in ({"record_type": "A"}, {"resolver": "google"}):
        request = CreateMeasurementRequest(target="example.com:443", kind=kind, **extra)  # type: ignore[arg-type]
        with pytest.raises(PlanError) as caught:
            plan_measurement(request, parse_target("example.com:443"), RuntimeSettings())
        assert caught.value.code == "invalid_option"


def test_unknown_record_types_and_resolvers_never_reach_a_node() -> None:
    for extra in ({"record_type": "AXFR"}, {"record_type": "ANY"}, {"resolver": "10.0.0.1"}):
        with pytest.raises(ValueError, match="validation error"):
            CreateMeasurementRequest(target="example.com", kind="dns", **extra)  # type: ignore[arg-type]


def test_a_dns_task_needs_a_record_type_and_a_name() -> None:
    base = {
        "task_id": "t",
        "attempt_id": "a",
        "kind": "dns",
        "expires_at": "2026-11-06T12:00:30Z",
        "options": {"timeout_ms": 5000, "record_type": "A"},
    }
    protocol.TaskFrame.model_validate({**base, "target": {"hostname": "example.com"}})
    with pytest.raises(ValueError, match="record_type"):
        protocol.TaskFrame.model_validate(
            {**base, "options": {"timeout_ms": 5000}, "target": {"hostname": "example.com"}}
        )
    with pytest.raises(ValueError, match="hostname"):
        protocol.TaskFrame.model_validate({**base, "target": {"ip": "1.1.1.1"}})


# ---------------------------------------------------------------------------------------------
# The detail of a sample
# ---------------------------------------------------------------------------------------------

DETAIL: dict[str, Any] = {
    "resolver": "1.1.1.1",
    "record_type": "MX",
    "rcode": "NOERROR",
    "authoritative": False,
    "via_tcp": False,
    "truncated": False,
    "answers": [{"name": "example.com", "type": "MX", "ttl": 3600, "data": "10 mail.example.com"}],
    "redacted": 0,
}


def test_a_valid_detail_is_kept_and_an_invalid_one_is_dropped() -> None:
    parsed = protocol.parse_detail("dns", DETAIL)
    assert isinstance(parsed, protocol.DnsDetail) and parsed.answers[0].data.startswith("10 mail")
    assert protocol.parse_detail("dns", {**DETAIL, "record_type": "AXFR"}) is None
    assert protocol.parse_detail("dns", {**DETAIL, "answers": [{"name": "x", "type": "A"}]}) is None
    too_many = [{"name": "e.com", "type": "A", "ttl": 1, "data": "1.1.1.1"}] * 33
    assert protocol.parse_detail("dns", {**DETAIL, "answers": too_many}) is None


def test_text_from_an_answer_cannot_carry_control_characters() -> None:
    answers = [{"name": "e.com", "type": "TXT", "ttl": 1, "data": "ok\x00\x1b[31m red\nline"}]
    parsed = protocol.parse_detail("dns", {**DETAIL, "answers": answers})
    assert isinstance(parsed, protocol.DnsDetail)
    assert parsed.answers[0].data == "ok??[31m red?line"
    assert "é" in protocol.DnsAnswerRecord(name="e.com", type="TXT", ttl=1, data="café").data


def test_a_dns_answer_fits_in_a_frame() -> None:
    """A full answer (32 records) must survive the size limit that drops oversized details."""
    records = [
        {"name": f"host{n}.example.com", "type": "TXT", "ttl": 60, "data": "v=" + "x" * 180}
        for n in range(32)
    ]
    frame = protocol.node_to_backend.validate_python(
        {
            "type": "sample",
            "task_id": "t",
            "attempt_id": "a",
            "seq": 0,
            "status": "ok",
            "rtt_us": 5,
            "detail": {**DETAIL, "answers": records},
        }
    )
    assert isinstance(frame, protocol.SampleFrame) and frame.detail is not None


# ---------------------------------------------------------------------------------------------
# The whole path, with a node that speaks the protocol
# ---------------------------------------------------------------------------------------------


def test_the_backend_never_resolves_the_name_so_a_missing_name_can_be_measured(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=KINDS_WITH_DNS)
    try:
        # the test resolver raises for names it does not know: a ping or tcp check would be refused
        refused = api.post("/api/v1/measurements", {"target": "no-such-name.example.com"})
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "dns_failure"

        measurement = _start(
            api, target="no-such-name.example.com", kind="dns", record_type="TXT", resolver="google"
        )
        assert measurement["kind"] == "dns" and measurement["dns_mode"] == "regional"
        assert measurement["target"]["ip"] is None
        assert measurement["options"]["record_type"] == "TXT"
        assert measurement["options"]["resolver"] == "google"
        task = _task(session)
        assert task["kind"] == "dns"
        assert task["target"] == {"hostname": "no-such-name.example.com", "family": "auto"}
        assert task["options"] == {
            "count": 1,
            "interval_ms": 1000,
            "timeout_ms": 5000,
            "record_type": "TXT",
            "resolver_ip": "8.8.8.8",
        }
        detail = {**DETAIL, "resolver": "8.8.8.8", "record_type": "TXT", "rcode": "NXDOMAIN"}
        detail["answers"] = []
        _send(session, task, type="sample", seq=0, status="ok", rtt_us=9_500, detail=detail)
        _send(session, task, type="task_result", status="completed", resolved_ips=[])
        barrier(session)

        done = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        assert done["status"] == "completed"
        node = done["tasks"][0]
        assert node["status"] == "completed" and node["tested_ip"] is None
        sample = node["samples"][0]
        assert sample["status"] == "ok" and sample["rtt_ms"] == 9.5
        assert sample["dns"]["rcode"] == "NXDOMAIN" and sample["dns"]["answers"] == []
        assert node["summary"]["received"] == 1 and node["summary"]["median_ms"] == 9.5
    finally:
        ws.__exit__(None, None, None)


def test_a_name_that_is_a_private_address_or_internal_is_refused_before_any_node(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential, kinds=KINDS_WITH_DNS)
    try:
        for target, code in [
            ("93.184.216.34", "invalid_target"),
            ("intranet.corp", "blocked_by_policy"),
            ("printer.home.arpa", "blocked_by_policy"),
            ("localhost", "blocked_by_policy"),
            ("nas", "invalid_target"),
        ]:
            response = api.post("/api/v1/measurements", {"target": target, "kind": "dns"})
            assert response.status_code == 422, target
            assert response.json()["error"]["code"] == code, target
    finally:
        ws.__exit__(None, None, None)


def test_the_blocklist_applies_to_names_asked_about(api: Api) -> None:
    api.become_admin()
    assert (
        api.patch(
            "/api/v1/admin/settings", {"target_denylist": ["blocked.example.com"]}
        ).status_code
        == 200
    )
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential, kinds=KINDS_WITH_DNS)
    try:
        response = api.post(
            "/api/v1/measurements", {"target": "www.blocked.example.com", "kind": "dns"}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "blocked_by_policy"
    finally:
        ws.__exit__(None, None, None)


def test_private_answers_from_the_nodes_own_resolver_are_never_stored(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=KINDS_WITH_DNS)
    try:
        measurement = _start(api, target="example.com", kind="dns")
        task = _task(session)
        assert "resolver_ip" not in task["options"]
        # a node that forgot to leave them out
        answers = [
            {"name": "example.com", "type": "A", "ttl": 60, "data": "93.184.216.34"},
            {"name": "example.com", "type": "A", "ttl": 60, "data": "192.168.7.7"},
            {"name": "example.com", "type": "AAAA", "ttl": 60, "data": "fd00::1"},
            {"name": "example.com", "type": "TXT", "ttl": 60, "data": "v=spf1 -all"},
        ]
        detail = {**DETAIL, "resolver": "system", "record_type": "A", "answers": answers}
        _send(session, task, type="sample", seq=0, status="ok", rtt_us=4_000, detail=detail)
        _send(session, task, type="task_result", status="completed")
        barrier(session)

        stored = api.get(f"/api/v1/measurements/{measurement['id']}").json()["tasks"][0]
        dns = stored["samples"][0]["dns"]
        assert dns["resolver"] == "system" and dns["redacted"] == 2
        assert [a["data"] for a in dns["answers"]] == ["93.184.216.34", "v=spf1 -all"]
        assert "192.168" not in json.dumps(stored) and "fd00" not in json.dumps(stored)
    finally:
        ws.__exit__(None, None, None)


def test_a_public_resolvers_private_looking_answers_are_data_and_stay(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=KINDS_WITH_DNS)
    try:
        measurement = _start(api, target="example.com", kind="dns", resolver="cloudflare")
        task = _task(session)
        answers = [{"name": "dev.example.com", "type": "A", "ttl": 60, "data": "127.0.0.1"}]
        detail = {**DETAIL, "resolver": "1.1.1.1", "record_type": "A", "answers": answers}
        _send(session, task, type="sample", seq=0, status="ok", rtt_us=4_000, detail=detail)
        _send(session, task, type="task_result", status="completed")
        barrier(session)
        dns = api.get(f"/api/v1/measurements/{measurement['id']}").json()["tasks"][0]["samples"][0][
            "dns"
        ]
        assert dns["answers"][0]["data"] == "127.0.0.1" and dns["redacted"] == 0
    finally:
        ws.__exit__(None, None, None)


def test_a_resolver_that_is_not_a_public_address_is_not_shown(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=KINDS_WITH_DNS)
    try:
        measurement = _start(api, target="example.com", kind="dns", resolver="google")
        task = _task(session)
        detail = {**DETAIL, "resolver": "10.1.2.3 (the office resolver)", "answers": []}
        _send(session, task, type="sample", seq=0, status="ok", rtt_us=4_000, detail=detail)
        _send(session, task, type="task_result", status="completed")
        barrier(session)
        dns = api.get(f"/api/v1/measurements/{measurement['id']}").json()["tasks"][0]["samples"][0][
            "dns"
        ]
        assert dns["resolver"] == "unknown"
    finally:
        ws.__exit__(None, None, None)


def test_only_nodes_that_advertise_dns_get_a_dns_check(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential, kinds=ALL_KINDS)  # a v0.3 node
    try:
        refused = api.post("/api/v1/measurements", {"target": "example.com", "kind": "dns"})
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "kind_not_supported"
    finally:
        ws.__exit__(None, None, None)


def test_dns_checks_can_be_turned_off(api: Api) -> None:
    api.become_admin()
    kinds = ["ping", "tcp", "http", "traceroute"]
    assert api.patch("/api/v1/admin/settings", {"enabled_kinds": kinds}).status_code == 200
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential, kinds=KINDS_WITH_DNS)
    try:
        refused = api.post("/api/v1/measurements", {"target": "example.com", "kind": "dns"})
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "kind_disabled"
    finally:
        ws.__exit__(None, None, None)


def test_a_new_deployment_has_dns_enabled(api: Api) -> None:
    api.become_admin()
    assert "dns" in api.get("/api/v1/admin/settings").json()["enabled_kinds"]
