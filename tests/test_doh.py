"""DNS over HTTPS: which resolvers may be asked that way, what a node is told, and what is kept."""

from __future__ import annotations

import threading
from typing import Any

import pytest

from ipping_backend.measurements import render
from ipping_backend.measurements.plan import DOH_RESOLVERS, PlanError, plan_measurement
from ipping_backend.measurements.schemas import CreateMeasurementRequest, MeasurementOut
from ipping_backend.nodes import protocol
from ipping_backend.runtime_settings import RuntimeSettings
from ipping_backend.security.targets import parse_target

from .conftest import Api
from .test_api import barrier, connect
from .test_api_keys import _as_script, _key
from .test_dns import DETAIL, KINDS_WITH_DNS
from .test_probe_kinds import _send, _start, _task


def _plan(target: str, **extra: Any) -> Any:
    request = CreateMeasurementRequest(target=target, kind="dns", **extra)
    return plan_measurement(request, parse_target(target), RuntimeSettings())


@pytest.mark.parametrize(
    ("resolver", "address"),
    [("cloudflare", "1.1.1.1"), ("google", "8.8.8.8"), ("quad9", "9.9.9.9")],
)
def test_a_public_resolver_can_be_asked_over_https(resolver: str, address: str) -> None:
    plan = _plan("example.com", resolver=resolver, transport="doh", record_type="TXT")
    host, path = DOH_RESOLVERS[resolver]
    assert plan.options["transport"] == "doh"
    assert (plan.options["doh_host"], plan.options["doh_path"]) == (host, "/dns-query")
    assert plan.options["resolver_ip"] == address and plan.options["record_type"] == "TXT"


def test_plain_dns_is_unchanged_whether_or_not_udp_is_said() -> None:
    for extra in ({}, {"transport": "udp"}):
        plan = _plan("example.com", resolver="cloudflare", **extra)
        assert "transport" not in plan.options and "doh_host" not in plan.options


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ({}, "public resolver"),  # the nodes' own resolvers cannot be asked over HTTPS
        ({"resolver": "system"}, "public resolver"),
    ],
)
def test_resolvers_that_cannot_be_asked_over_https_say_why(
    extra: dict[str, str], needle: str
) -> None:
    with pytest.raises(PlanError) as caught:
        _plan("example.com", transport="doh", **extra)
    assert caught.value.code == "invalid_option" and needle in caught.value.message


def test_only_a_dns_check_takes_a_transport() -> None:
    request = CreateMeasurementRequest(target="example.com", kind="ping", transport="doh")
    with pytest.raises(PlanError) as caught:
        plan_measurement(request, parse_target("example.com"), RuntimeSettings())
    assert caught.value.code == "invalid_option"


def test_a_transport_nobody_knows_never_reaches_a_node() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        CreateMeasurementRequest(target="example.com", kind="dns", transport="dot")  # type: ignore[arg-type]


def test_the_detail_and_the_task_options_carry_the_transport() -> None:
    detail = protocol.parse_detail("dns", {**DETAIL, "transport": "doh"})
    assert isinstance(detail, protocol.DnsDetail) and detail.transport == "doh"
    assert protocol.parse_detail("dns", {**DETAIL, "transport": "carrier-pigeon"}) is None
    plain = protocol.parse_detail("dns", DETAIL)
    assert isinstance(plain, protocol.DnsDetail) and plain.transport is None
    options = protocol.TaskOptions.model_validate(
        {"timeout_ms": 5000, "transport": "doh", "doh_host": "dns.google", "doh_path": "/dns-query"}
    )
    assert options.transport == "doh" and options.doh_path == "/dns-query"
    with pytest.raises(ValueError):  # noqa: PT011
        protocol.TaskOptions.model_validate({"timeout_ms": 5000, "doh_path": "/" + "a" * 70})


def test_the_node_is_told_where_to_ask_and_the_answer_says_it_came_over_https(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential, kinds=KINDS_WITH_DNS)
    try:
        measurement = _start(
            api, target="example.com", kind="dns", resolver="google", transport="doh"
        )
        assert measurement["options"]["transport"] == "doh"
        task = _task(session)
        assert task["options"] == {
            "count": 1, "interval_ms": 1000, "timeout_ms": 5000, "record_type": "A",
            "resolver_ip": "8.8.8.8", "transport": "doh", "doh_host": "dns.google", "doh_path": "/dns-query",
        }  # fmt: skip
        _send(session, task, type="sample", seq=0, status="ok", rtt_us=31_000,
              detail={**DETAIL, "resolver": "8.8.8.8", "transport": "doh"})  # fmt: skip
        _send(session, task, type="task_result", status="completed", resolved_ips=[])
        barrier(session)

        done = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        assert done["tasks"][0]["samples"][0]["dns"]["transport"] == "doh"
        text = render.render_text(MeasurementOut.model_validate(done))
        assert "(DoH)" in text, text
    finally:
        ws.__exit__(None, None, None)


def test_a_refused_combination_is_a_clear_error_before_any_node(api: Api) -> None:
    api.become_admin()
    response = api.post(
        "/api/v1/measurements",
        {"target": "example.com", "kind": "dns", "resolver": "system", "transport": "doh"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_option"
    assert "public resolver" in response.json()["error"]["message"]


def test_the_text_endpoint_passes_the_transport_on(api: Api) -> None:
    api.become_admin()
    key = _key(api)["key"]
    _, credential = api.enroll_node("Dubai")
    ws, session = connect(api, credential, kinds=KINDS_WITH_DNS)
    try:
        headers = _as_script(api, key)
        result: dict[str, Any] = {}

        def call() -> None:
            result["response"] = api.client.get(
                "/api/v1/text?target=example.com&kind=dns&resolver=cloudflare&transport=doh&wait=10",
                headers=headers,
            )

        thread = threading.Thread(target=call)
        thread.start()
        task = _task(session)
        assert (
            task["options"]["transport"] == "doh"
            and task["options"]["doh_host"] == "cloudflare-dns.com"
        )
        _send(session, task, type="sample", seq=0, status="ok", rtt_us=20_000,
              detail={**DETAIL, "resolver": "1.1.1.1", "transport": "doh"})  # fmt: skip
        _send(session, task, type="task_result", status="completed", resolved_ips=[])
        barrier(session)
        thread.join(15)
        assert result["response"].status_code == 200, result["response"].text
        assert "(DoH)" in result["response"].text
    finally:
        ws.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("version", "speaks"),
    [
        ("0.5.1", False), ("0.6.0", True), ("0.6.1", True), ("0.10.0", True), ("1.0.0", True),
        ("0.6.0-rc1", True), ("0.6", False), ("", False), (None, False), ("garbage", False),
    ],
)  # fmt: skip
def test_which_node_versions_speak_which_transport(version: str | None, speaks: bool) -> None:
    assert protocol.node_speaks(version, "doh") is speaks
    assert protocol.node_speaks(version, "quic") is speaks
    for plain in (None, "udp", "tcp"):  # the default transports are every node's
        assert protocol.node_speaks(version, plain) is True


def test_a_node_that_ignores_the_transport_is_not_asked_for_it(api: Api) -> None:
    """A 0.5.1 node would answer a DoH question with plain DNS and a QUIC one over TCP: found when
    upgrading a real v0.5.1 stack, where both came back "ok" and nothing said they were not
    what was asked."""
    api.become_admin()
    api.patch("/api/v1/admin/settings", {"max_concurrent_per_session": 50})
    _, old = api.enroll_node("Old")
    _, current = api.enroll_node("Current")
    old_ws, old_session = connect(api, old, kinds=KINDS_WITH_DNS, version="0.5.1")
    ws, session = connect(api, current, kinds=KINDS_WITH_DNS)
    try:
        measurement = _start(
            api, target="example.com", kind="dns", resolver="google", transport="doh"
        )
        assert [t["node"]["name"] for t in measurement["tasks"]] == ["Current"]
        assert _task(session)["options"]["transport"] == "doh"
        # Plain DNS is asked of both.
        plain = _start(api, target="example.com", kind="dns", resolver="google")
        assert sorted(t["node"]["name"] for t in plain["tasks"]) == ["Current", "Old"]
        # Asking the old node for it by name says it cannot.
        named = api.post(
            "/api/v1/measurements",
            {"target": "example.com", "kind": "dns", "resolver": "google", "transport": "doh",
             "node_ids": [measurement["tasks"][0]["node"]["id"], _node_id(api, "Old")]},
        )  # fmt: skip
        assert named.status_code == 409 and named.json()["error"]["code"] == "node_unavailable"
    finally:
        ws.__exit__(None, None, None)
        old_ws.__exit__(None, None, None)


def test_when_every_node_is_too_old_the_error_says_what_to_update_for(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential, kinds=KINDS_WITH_DNS, version="0.5.1")
    try:
        refused = api.post(
            "/api/v1/measurements",
            {"target": "example.com", "kind": "dns", "resolver": "cloudflare", "transport": "doh"},
        )
        assert refused.status_code == 409
        error = refused.json()["error"]
        assert error["code"] == "kind_not_supported"
        assert "dns measurement over DoH" in error["message"] and "updated" in error["message"]
    finally:
        ws.__exit__(None, None, None)


def _node_id(api: Api, name: str) -> str:
    nodes = api.get("/api/v1/admin/nodes").json()
    return next(n["id"] for n in nodes if n["name"] == name)


@pytest.mark.parametrize(
    ("version", "speaks"),
    [("0.6.0", False), ("0.6.9", False), ("0.7.0", True), ("0.10.0", True), ("1.0.0", True), (None, False)],
)  # fmt: skip
def test_quad9_needs_a_node_that_speaks_http2(version: str | None, speaks: bool) -> None:
    assert protocol.node_speaks(version, "doh", "quad9") is speaks
    # the other resolvers need only DNS over HTTPS (0.6.0, every version listed here but None), and plain DNS nothing
    assert protocol.node_speaks(version, "doh", "cloudflare") is (version is not None)
    assert protocol.node_speaks(version, None, "quad9") is True


def test_quad9_goes_only_to_nodes_that_speak_http2(api: Api) -> None:
    api.become_admin()
    api.patch("/api/v1/admin/settings", {"max_concurrent_per_session": 50})
    _, previous = api.enroll_node("Previous")
    _, current = api.enroll_node("Current")
    previous_ws, _ = connect(api, previous, kinds=KINDS_WITH_DNS, version="0.6.0")
    ws, session = connect(api, current, kinds=KINDS_WITH_DNS, version="0.7.0")
    try:
        quad9 = _start(api, target="example.com", kind="dns", resolver="quad9", transport="doh")
        assert [t["node"]["name"] for t in quad9["tasks"]] == ["Current"]
        task = _task(session)
        assert task["options"]["doh_host"] == "dns.quad9.net"
        assert task["options"]["resolver_ip"] == "9.9.9.9"
        # DNS over HTTPS to a resolver that does not need HTTP/2 still goes to both
        google = _start(api, target="example.com", kind="dns", resolver="google", transport="doh")
        assert sorted(t["node"]["name"] for t in google["tasks"]) == ["Current", "Previous"]
    finally:
        ws.__exit__(None, None, None)
        previous_ws.__exit__(None, None, None)


def test_when_no_node_speaks_http2_the_error_says_so(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, _ = connect(api, credential, kinds=KINDS_WITH_DNS, version="0.6.0")
    try:
        refused = api.post(
            "/api/v1/measurements",
            {"target": "example.com", "kind": "dns", "resolver": "quad9", "transport": "doh"},
        )
        assert refused.status_code == 409
        error = refused.json()["error"]
        assert error["code"] == "kind_not_supported"
        assert "to quad9" in error["message"] and "HTTP/2" in error["message"]
    finally:
        ws.__exit__(None, None, None)
