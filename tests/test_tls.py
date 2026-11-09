"""Certificate checks: what is asked of a node, what is kept, and when a watch calls one good."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ipping_backend.measurements import render
from ipping_backend.measurements.plan import PlanError, plan_measurement, probe_cost, sample_limit
from ipping_backend.measurements.schemas import CreateMeasurementRequest, MeasurementOut, SampleOut
from ipping_backend.nodes import protocol
from ipping_backend.nodes.protocol import TlsDetail, TlsInfo
from ipping_backend.runtime_settings import RuntimeSettings
from ipping_backend.security.targets import parse_target
from ipping_backend.watches import evaluate

from .conftest import Api
from .test_api import connect
from .test_probe_kinds import ALL_KINDS, _complete, _send, _start, _task
from .test_watches import _measurement, _task_out

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


def _plan(target: str, **extra: Any) -> Any:
    request = CreateMeasurementRequest(target=target, kind="tls", **extra)
    return plan_measurement(request, parse_target(target), RuntimeSettings())


# ---------------------------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------------------------


def test_a_certificate_check_is_one_handshake_on_a_port() -> None:
    plan = _plan("example.com")
    assert plan.port == 443 and plan.options["port"] == 443
    assert plan.options["count"] == 1 and plan.options["timeout_ms"] == 8000
    assert "scheme" not in plan.options and "path" not in plan.options
    assert sample_limit("tls", plan.options) == 1
    assert probe_cost("tls", plan.options) == 2
    assert plan.deadline_s >= 18

    # the port can come from host:port, a URL or the field; the field wins
    assert _plan("example.com:8443").port == 8443
    assert _plan("https://example.com:993/").port == 993
    assert _plan("example.com:8443", port=587).port == 587


@pytest.mark.parametrize(
    ("target", "extra", "code"),
    [
        ("example.com:6379", {}, "port_not_allowed"),
        ("example.com", {"port": 23}, "port_not_allowed"),
        ("example.com", {"count": 3}, "invalid_option"),
        ("example.com", {"follow_redirects": True}, "invalid_option"),
    ],
)
def test_certificate_plans_that_make_no_sense_are_refused(
    target: str, extra: dict[str, Any], code: str
) -> None:
    with pytest.raises(PlanError) as caught:
        _plan(target, **extra)
    assert caught.value.code == code


def test_tls_is_a_kind_a_deployment_can_turn_off() -> None:
    request = CreateMeasurementRequest(target="example.com", kind="tls")
    settings = RuntimeSettings(enabled_kinds=["ping", "http"])
    with pytest.raises(PlanError) as caught:
        plan_measurement(request, parse_target("example.com"), settings)
    assert caught.value.code == "kind_disabled"
    assert "tls" in RuntimeSettings().enabled_kinds


# ---------------------------------------------------------------------------------------------
# What a node may send
# ---------------------------------------------------------------------------------------------


def test_a_certificate_detail_is_bounded_and_cleaned() -> None:
    detail = protocol.parse_detail(
        "tls",
        {
            "server_name": "exam\x1b[0mple.com",
            "total_us": 31_000,
            "tls": {"version": "TLSv1.3", "verified": False, "verify_error": "expired\n"},
            "names": ["a.example.com", "evil\x07.example.com", "*.example.com"],
            "chain": 2,
        },
    )
    assert isinstance(detail, TlsDetail)
    assert detail.server_name == "exam?[0mple.com"
    assert detail.names == ["a.example.com", "evil?.example.com", "*.example.com"]
    assert detail.tls is not None and detail.tls.verify_error == "expired?"

    assert protocol.parse_detail("tls", {"names": ["x"] * 25}) is None, "too many names"
    assert protocol.parse_detail("tls", {"chain": 99}) is None
    assert protocol.parse_detail("tls", {"total_us": -1}) is None
    assert protocol.parse_detail("ping", {"names": []}) is None


def test_a_tls_task_needs_a_port() -> None:
    task = {
        "type": "task", "task_id": "t", "attempt_id": "a", "kind": "tls",
        "target": {"ip": "93.184.216.34", "family": "auto", "server_name": "example.com"},
        "options": {"timeout_ms": 8000},
        "expires_at": "2026-11-08T12:00:30Z",
    }  # fmt: skip
    with pytest.raises(ValueError, match="needs a port"):
        protocol.backend_to_node.validate_python(task)
    task["target"]["port"] = 443  # type: ignore[index]
    parsed = protocol.backend_to_node.validate_python(task)
    assert isinstance(parsed, protocol.TaskFrame) and parsed.kind == "tls"


# ---------------------------------------------------------------------------------------------
# When a watch calls a certificate good
# ---------------------------------------------------------------------------------------------


def _cert_task(days: float | None, verified: bool = True) -> Any:
    info = TlsInfo(
        version="TLSv1.3",
        verified=verified,
        verify_error=None if verified else "the certificate has expired",
        not_after=None if days is None else NOW + timedelta(days=days),
    )
    return _task_out(
        samples=[
            SampleOut(
                seq=0,
                status="ok" if verified else "error",
                rtt_ms=30.0 if verified else None,
                tls=TlsDetail(tls=info, names=["example.com"], chain=2),
            )
        ]
    )


def test_a_certificate_is_good_when_it_is_trusted_and_not_about_to_expire() -> None:
    assert evaluate.task_ok("tls", _cert_task(90), NOW)
    assert evaluate.task_ok("tls", _cert_task(14), NOW), "exactly the warning limit is still fine"
    assert not evaluate.task_ok("tls", _cert_task(13.9), NOW)
    assert not evaluate.task_ok("tls", _cert_task(90, verified=False), NOW)
    assert not evaluate.task_ok("tls", _cert_task(None), NOW), "no expiry date is not good"
    assert not evaluate.task_ok("tls", _task_out(), NOW), "no answer is not good"


def test_a_certificate_nearly_out_of_time_is_degraded_not_down() -> None:
    soon = evaluate.assess(_measurement("tls", [_cert_task(9.4), _cert_task(9.4)]), 80, NOW)
    assert soon.status == "degraded" and soon.nodes_ok == 0
    assert soon.note == "the certificate expires in 9 days"

    # an untrusted certificate, or none at all, is an outage
    assert evaluate.assess(_measurement("tls", [_cert_task(90, False)]), 80, NOW).status == "down"
    assert evaluate.assess(_measurement("tls", [_task_out()]), 80, NOW).status == "down"
    # and a healthy one is ok, with no note
    fine = evaluate.assess(_measurement("tls", [_cert_task(60)] * 3), 80, NOW)
    assert fine.status == "ok" and fine.note is None
    # the nodes disagree: the share that is good decides, as for any other kind
    mixed = evaluate.assess(_measurement("tls", [_cert_task(60), _cert_task(5)]), 80, NOW)
    assert mixed.status == "degraded" and mixed.nodes_ok == 1


# ---------------------------------------------------------------------------------------------
# Through a node
# ---------------------------------------------------------------------------------------------

CERT = {
    "server_name": "example.com",
    "connect_us": 9_000,
    "tls_us": 23_000,
    "total_us": 32_000,
    "tls": {
        "version": "TLSv1.3",
        "cipher": "TLS13_AES_256_GCM_SHA384",
        "subject": "CN=example.com",
        "issuer": "CN=Test CA",
        "not_before": "2026-10-01T00:00:00Z",
        "not_after": "2027-02-01T00:00:00Z",
        "verified": True,
    },
    "names": ["example.com", "www.example.com"],
    "chain": 2,
}


def test_the_node_is_asked_for_a_handshake_and_its_certificate_is_kept(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=ALL_KINDS + ("tls",))
    try:
        measurement = _start(api, target="example.com", kind="tls")
        assert measurement["kind"] == "tls" and measurement["options"]["port"] == 443
        task = _task(session)
        assert task["kind"] == "tls"
        # the address is pinned, the name is presented to the server
        assert task["target"] == {
            "ip": "93.184.216.34",
            "family": "auto",
            "port": 443,
            "server_name": "example.com",
        }
        assert task["options"]["timeout_ms"] == 8000
        _send(session, task, type="sample", seq=0, status="ok", rtt_us=32_000, detail=CERT)
        _complete(session, task)

        final = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        done = final["tasks"][0]
        cert = done["samples"][0]["tls"]
        assert cert["tls"]["verified"] is True and cert["tls"]["issuer"] == "CN=Test CA"
        assert cert["names"] == ["example.com", "www.example.com"] and cert["chain"] == 2
        assert done["summary"]["received"] == 1 and done["summary"]["median_ms"] == 32.0
        assert final["stats"]["nodes_responding"] == 1

        text = render.render_text(MeasurementOut.model_validate(final))
        assert text.startswith("Certificate example.com:443")
        assert "trusted  TLSv1.3  expires 2027-02-01" in text, text
    finally:
        ws.__exit__(None, None, None)


def test_a_certificate_that_is_not_trusted_still_shows_what_it_is(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=ALL_KINDS + ("tls",))
    try:
        measurement = _start(api, target="example.com:8443", kind="tls")
        task = _task(session)
        assert task["target"]["port"] == 8443
        bad = json.loads(json.dumps(CERT))
        bad["tls"].update(verified=False, verify_error="the certificate has expired")
        _send(
            session, task, type="sample", seq=0, status="error",
            error_code="tls_certificate_invalid", detail=bad,
        )  # fmt: skip
        _complete(session, task)

        final = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        sample = final["tasks"][0]["samples"][0]
        assert sample["status"] == "error" and sample["error_code"] == "tls_certificate_invalid"
        assert sample["tls"]["tls"]["verify_error"] == "the certificate has expired"
        assert sample["tls"]["names"] == ["example.com", "www.example.com"]
        text = render.render_text(MeasurementOut.model_validate(final))
        assert "NOT trusted (the certificate has expired)" in text, text
    finally:
        ws.__exit__(None, None, None)


def test_a_bare_address_presents_no_name_and_a_closed_port_is_refused_up_front(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=ALL_KINDS + ("tls",))
    try:
        _start(api, target="93.184.216.34", kind="tls")
        task = _task(session)
        assert task["target"] == {"ip": "93.184.216.34", "family": "auto", "port": 443}

        refused = api.post("/api/v1/measurements", {"target": "example.com:6379", "kind": "tls"})
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "port_not_allowed"
    finally:
        ws.__exit__(None, None, None)


def test_a_node_without_certificate_checks_is_not_asked_for_one(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential, kinds=ALL_KINDS)  # an older node: no tls
    try:
        response = api.post("/api/v1/measurements", {"target": "example.com", "kind": "tls"})
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "kind_not_supported"
    finally:
        ws.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------
# QUIC
# ---------------------------------------------------------------------------------------------


def test_a_certificate_can_be_checked_over_quic_and_the_wrong_transports_are_refused() -> None:
    plan = _plan("example.com", transport="quic")
    assert plan.options["transport"] == "quic" and plan.options["port"] == 443
    assert "transport" not in _plan("example.com").options
    assert "transport" not in _plan("example.com", transport="tcp").options
    for wrong in ("udp", "doh"):
        with pytest.raises(PlanError) as caught:
            _plan("example.com", transport=wrong)
        assert caught.value.code == "invalid_option" and "tcp or quic" in caught.value.message
    # and QUIC is not a way to ask DNS, nor a transport of anything else
    for kind, transport in (("dns", "quic"), ("dns", "tcp"), ("ping", "quic")):
        request = CreateMeasurementRequest(target="example.com", kind=kind, transport=transport)  # type: ignore[arg-type]
        with pytest.raises(PlanError) as caught:
            plan_measurement(request, parse_target("example.com"), RuntimeSettings())
        assert caught.value.code == "invalid_option"


def test_the_detail_carries_the_transport_and_the_protocol_agreed() -> None:
    detail = protocol.parse_detail("tls", {**CERT, "transport": "quic", "alpn": "h3"})
    assert isinstance(detail, TlsDetail) and (detail.transport, detail.alpn) == ("quic", "h3")
    assert protocol.parse_detail("tls", {**CERT, "transport": "smoke-signals"}) is None
    assert protocol.parse_detail("tls", {**CERT, "alpn": "x" * 30}) is None
    cleaned = protocol.parse_detail("tls", {**CERT, "alpn": "h3\x1b"})
    assert isinstance(cleaned, TlsDetail) and cleaned.alpn == "h3?"
    options = protocol.TaskOptions.model_validate({"timeout_ms": 8000, "transport": "quic"})
    assert options.transport == "quic"


def test_a_quic_check_reaches_the_node_and_its_result_says_it_was_quic(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=ALL_KINDS + ("tls",))
    try:
        measurement = _start(api, target="example.com", kind="tls", transport="quic")
        assert measurement["options"]["transport"] == "quic"
        task = _task(session)
        assert task["options"] == {
            "count": 1,
            "interval_ms": 1000,
            "timeout_ms": 8000,
            "transport": "quic",
        }
        quic = {**CERT, "transport": "quic", "alpn": "h3"}
        _send(session, task, type="sample", seq=0, status="ok", rtt_us=32_000, detail=quic)
        _complete(session, task)
        final = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        sample = final["tasks"][0]["samples"][0]["tls"]
        assert (sample["transport"], sample["alpn"]) == ("quic", "h3")
        text = render.render_text(MeasurementOut.model_validate(final))
        assert "(QUIC, h3)" in text, text
    finally:
        ws.__exit__(None, None, None)


def test_a_node_without_quic_is_not_asked_for_a_quic_check(api: Api) -> None:
    """A 0.5.1 node would do the handshake over TCP and report it as an ordinary certificate
    check: right certificate, wrong question."""
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=ALL_KINDS + ("tls",), version="0.5.1")
    try:
        refused = api.post(
            "/api/v1/measurements", {"target": "example.com", "kind": "tls", "transport": "quic"}
        )
        assert refused.status_code == 409, refused.text
        error = refused.json()["error"]
        assert error["code"] == "kind_not_supported"
        assert "tls measurement over QUIC" in error["message"]
        # The ordinary check still goes to it.
        _start(api, target="example.com", kind="tls")
        assert _task(session)["kind"] == "tls"
    finally:
        ws.__exit__(None, None, None)
