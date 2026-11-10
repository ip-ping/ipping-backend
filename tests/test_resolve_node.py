"""Who resolves a hostname: each node by default, or one node chosen to do it for everybody."""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest
from sqlalchemy import func, select

from ipping_backend.measurements import engine as engine_module
from ipping_backend.measurements.plan import Plan
from ipping_backend.models import Measurement, MeasurementTask, Sample

from .conftest import Api
from .test_api import barrier, connect

PING = ("ping",)
PING_AND_DNS = ("ping", "dns")


def _answer(
    rtype: str = "A", *addresses: str, rcode: str = "NOERROR", name: str = "example.com"
) -> dict[str, Any]:
    """What a node's own resolvers say, as the node reports it."""
    return {
        "resolver": "system",
        "record_type": rtype,
        "rcode": rcode,
        "answers": [{"name": name, "type": rtype, "ttl": 60, "data": a} for a in addresses],
    }


def _receive(session: Any) -> dict[str, Any]:
    task = json.loads(session.receive_text())
    assert task["type"] == "task"
    session.send_text(
        json.dumps(
            {"type": "task_ack", "task_id": task["task_id"], "attempt_id": task["attempt_id"]}
        )
    )
    return task  # type: ignore[no-any-return]


def _reply(session: Any, task: dict[str, Any], detail: dict[str, Any]) -> None:
    ids = {"task_id": task["task_id"], "attempt_id": task["attempt_id"]}
    session.send_text(
        json.dumps(
            {"type": "sample", **ids, "seq": 0, "status": "ok", "rtt_us": 4_000, "detail": detail}
        )
    )
    session.send_text(json.dumps({"type": "task_result", **ids, "status": "completed"}))
    barrier(session)


def _post_in_background(api: Api, body: dict[str, Any]) -> tuple[threading.Thread, dict[str, Any]]:
    """Creating a measurement waits for the node that resolves the name, which is played from the
    test's own thread, so the request has to be made from another."""
    result: dict[str, Any] = {}

    def call() -> None:
        result["response"] = api.post("/api/v1/measurements", body)

    thread = threading.Thread(target=call)
    thread.start()
    return thread, result


def _finish(thread: threading.Thread, result: dict[str, Any]) -> Any:
    thread.join(20)
    assert not thread.is_alive()
    return result["response"]


def _counts(api: Api) -> tuple[int, int, int]:
    async def count() -> tuple[int, int, int]:
        found = []
        async with api.client.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
            for table in (Measurement, MeasurementTask, Sample):
                found.append((await db.scalar(select(func.count()).select_from(table))) or 0)
        return found[0], found[1], found[2]

    return api.client.portal.call(count)  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------------------------
# The default: each node resolves it
# ---------------------------------------------------------------------------------------------


def test_each_node_resolves_the_name_unless_one_is_asked_to(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential)
    try:
        created = api.post("/api/v1/measurements", {"target": "example.com"})
        assert created.status_code == 201, created.text
        measurement = created.json()
        assert measurement["dns_mode"] == "regional" and measurement["resolved_by"] is None
        assert measurement["target"]["ip"] is None
        task = _receive(session)
        assert task["target"] == {"hostname": "example.com", "family": "auto"}
    finally:
        ws.__exit__(None, None, None)


def test_an_address_is_the_same_for_every_node_whatever_the_mode(api: Api) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential)
    try:
        measurement = api.post("/api/v1/measurements", {"target": "93.184.216.34"}).json()
        assert measurement["dns_mode"] == "fixed" and measurement["target"]["ip"]
        assert _receive(session)["target"]["ip"] == "93.184.216.34"
    finally:
        ws.__exit__(None, None, None)


def test_the_backend_alone_resolving_it_is_still_what_fixed_without_a_node_means(
    api: Api,
) -> None:
    api.become_admin()
    _, credential = api.enroll_node()
    ws, session = connect(api, credential)
    try:
        measurement = api.post(
            "/api/v1/measurements", {"target": "example.com", "dns_mode": "fixed"}
        ).json()
        assert measurement["target"]["ip"] == "93.184.216.34" and measurement["resolved_by"] is None
        assert _receive(session)["target"]["ip"] == "93.184.216.34"
    finally:
        ws.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------
# One node resolves it
# ---------------------------------------------------------------------------------------------


def test_the_chosen_node_resolves_the_name_and_every_node_measures_what_it_found(
    api: Api,
) -> None:
    api.become_admin()
    chosen, chosen_cred = api.enroll_node("Tokyo", country="JP", city="Tokyo")
    _, other_cred = api.enroll_node("Frankfurt")
    ws_a, tokyo = connect(api, chosen_cred, kinds=PING_AND_DNS)
    ws_b, frankfurt = connect(api, other_cred, kinds=PING)
    try:
        before = _counts(api)
        thread, result = _post_in_background(
            api, {"target": "example.com", "dns_mode": "fixed", "resolve_node_id": chosen}
        )
        # the question goes to the chosen node, to its own resolvers, about the name as typed
        question = _receive(tokyo)
        assert question["kind"] == "dns"
        assert question["target"] == {"hostname": "example.com", "family": "auto"}
        assert question["options"]["record_type"] == "A"
        assert "resolver_ip" not in question["options"]  # its own resolvers, not a public one
        # the backend's own resolver says 93.184.216.34: what the node says is what is measured
        _reply(tokyo, question, _answer("A", "151.101.1.69"))

        created = _finish(thread, result)
        assert created.status_code == 201, created.text
        measurement = created.json()
        assert measurement["dns_mode"] == "fixed"
        assert measurement["target"]["ip"] == "151.101.1.69"
        assert measurement["resolved_by"]["id"] == chosen
        assert measurement["resolved_by"]["name"] == "Tokyo"
        assert measurement["resolved_by"]["city"] == "Tokyo"

        # every node, the one that resolved it too, is sent the address, not the name
        assert _receive(tokyo)["target"]["ip"] == "151.101.1.69"
        assert _receive(frankfurt)["target"]["ip"] == "151.101.1.69"

        shown = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        assert shown["resolved_by"]["name"] == "Tokyo"
        assert {t["tested_ip"] for t in shown["tasks"]} == {"151.101.1.69"}

        # the question was only a means: one measurement is left, with its two tasks and no samples
        assert _counts(api) == (before[0] + 1, before[1] + 2, before[2])
    finally:
        ws_a.__exit__(None, None, None)
        ws_b.__exit__(None, None, None)


def test_the_node_that_resolves_it_need_not_be_one_that_measures(api: Api) -> None:
    api.become_admin()
    chosen, chosen_cred = api.enroll_node("Tokyo")
    other, other_cred = api.enroll_node("Frankfurt")
    ws_a, tokyo = connect(api, chosen_cred, kinds=PING_AND_DNS)
    ws_b, frankfurt = connect(api, other_cred, kinds=PING)
    try:
        thread, result = _post_in_background(
            api,
            {
                "target": "example.com",
                "dns_mode": "fixed",
                "resolve_node_id": chosen,
                "node_ids": [other],
            },
        )
        _reply(tokyo, _receive(tokyo), _answer("A", "151.101.1.69"))
        created = _finish(thread, result)
        assert created.status_code == 201, created.text
        assert [t["node"]["name"] for t in created.json()["tasks"]] == ["Frankfurt"]
        assert _receive(frankfurt)["target"]["ip"] == "151.101.1.69"
    finally:
        ws_a.__exit__(None, None, None)
        ws_b.__exit__(None, None, None)


def test_ipv6_is_asked_for_when_asked_for_and_when_there_is_no_ipv4(api: Api) -> None:
    api.become_admin()
    chosen, credential = api.enroll_node("Tokyo")
    ws, tokyo = connect(api, credential, kinds=PING_AND_DNS)
    try:
        thread, result = _post_in_background(
            api,
            {
                "target": "example.com",
                "dns_mode": "fixed",
                "resolve_node_id": chosen,
                "family": "6",
            },
        )
        question = _receive(tokyo)
        assert question["options"]["record_type"] == "AAAA"
        _reply(tokyo, question, _answer("AAAA", "2606:4700::6810:84e5"))
        created = _finish(thread, result)
        assert created.status_code == 201, created.text
        assert created.json()["target"]["ip"] == "2606:4700::6810:84e5"
        _receive(tokyo)

        # "automatic" asks for IPv4 first, and for IPv6 only when there is none
        thread, result = _post_in_background(
            api, {"target": "example.com", "dns_mode": "fixed", "resolve_node_id": chosen}
        )
        first = _receive(tokyo)
        assert first["options"]["record_type"] == "A"
        _reply(tokyo, first, _answer("A"))
        second = _receive(tokyo)
        assert second["options"]["record_type"] == "AAAA"
        _reply(tokyo, second, _answer("AAAA", "2606:4700::6810:84e5"))
        created = _finish(thread, result)
        assert created.status_code == 201, created.text
        assert created.json()["target"]["ip"] == "2606:4700::6810:84e5"
    finally:
        ws.__exit__(None, None, None)


def test_an_address_that_is_not_public_is_refused_like_any_other(api: Api) -> None:
    api.become_admin()
    chosen, credential = api.enroll_node("Tokyo")
    ws, tokyo = connect(api, credential, kinds=PING_AND_DNS)
    try:
        before = _counts(api)
        thread, result = _post_in_background(
            api, {"target": "example.com", "dns_mode": "fixed", "resolve_node_id": chosen}
        )
        # the node's own resolver says 10.0.0.5: the node leaves it out of what it reports and counts it
        question = _receive(tokyo)
        detail = _answer("A", "151.101.1.69")
        detail["redacted"] = 1
        _reply(tokyo, question, detail)
        refused = _finish(thread, result)
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "blocked_by_policy"
        assert "Tokyo" in refused.json()["error"]["message"]

        # and a node that sends the private address anyway is not believed either
        thread, result = _post_in_background(
            api, {"target": "example.com", "dns_mode": "fixed", "resolve_node_id": chosen}
        )
        _reply(tokyo, _receive(tokyo), _answer("A", "10.0.0.5"))
        refused = _finish(thread, result)
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "blocked_by_policy"
        assert _counts(api) == before  # nothing is left of either attempt
    finally:
        ws.__exit__(None, None, None)


def test_the_blocklist_applies_to_what_the_node_found(api: Api) -> None:
    api.become_admin()
    api.patch("/api/v1/admin/settings", {"target_denylist": ["151.101.0.0/16"]})
    chosen, credential = api.enroll_node("Tokyo")
    ws, tokyo = connect(api, credential, kinds=PING_AND_DNS)
    try:
        thread, result = _post_in_background(
            api, {"target": "example.com", "dns_mode": "fixed", "resolve_node_id": chosen}
        )
        _reply(tokyo, _receive(tokyo), _answer("A", "151.101.1.69"))
        refused = _finish(thread, result)
        assert refused.status_code == 422
        assert refused.json()["error"]["code"] == "blocked_by_policy"
    finally:
        ws.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("detail", "code"),
    [
        (_answer("A", rcode="NXDOMAIN"), "dns_failure"),
        (_answer("A", rcode="SERVFAIL"), "dns_failure"),
    ],
)
def test_a_name_the_node_cannot_resolve_is_reported_with_the_node_named(
    api: Api, detail: dict[str, Any], code: str
) -> None:
    api.become_admin()
    chosen, credential = api.enroll_node("Tokyo")
    ws, tokyo = connect(api, credential, kinds=PING_AND_DNS)
    try:
        thread, result = _post_in_background(
            api, {"target": "example.com", "dns_mode": "fixed", "resolve_node_id": chosen}
        )
        _reply(tokyo, _receive(tokyo), detail)
        refused = _finish(thread, result)
        assert refused.status_code == 422 and refused.json()["error"]["code"] == code
        assert "Tokyo" in refused.json()["error"]["message"]
    finally:
        ws.__exit__(None, None, None)


def test_a_name_with_no_address_records_is_a_dns_failure(api: Api) -> None:
    api.become_admin()
    chosen, credential = api.enroll_node("Tokyo")
    ws, tokyo = connect(api, credential, kinds=PING_AND_DNS)
    try:
        thread, result = _post_in_background(
            api,
            {
                "target": "example.com",
                "dns_mode": "fixed",
                "resolve_node_id": chosen,
                "family": "4",
            },
        )
        _reply(tokyo, _receive(tokyo), _answer("A"))  # NOERROR, but nothing in it
        refused = _finish(thread, result)
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "dns_failure"
    finally:
        ws.__exit__(None, None, None)


def test_a_node_that_does_not_answer_gives_up_and_leaves_nothing_behind(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    short = Plan(
        {
            "count": 1,
            "interval_ms": 1000,
            "timeout_ms": 5000,
            "record_type": "A",
            "resolver": "system",
        },
        None,
        1,
    )
    monkeypatch.setattr(engine_module, "plan_lookup", lambda record_type: short)
    api.become_admin()
    chosen, credential = api.enroll_node("Tokyo")
    ws, tokyo = connect(api, credential, kinds=PING_AND_DNS)
    try:
        before = _counts(api)
        thread, result = _post_in_background(
            api, {"target": "example.com", "dns_mode": "fixed", "resolve_node_id": chosen}
        )
        question = _receive(tokyo)  # it takes the task and never answers
        assert question["kind"] == "dns"
        refused = _finish(thread, result)
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "dns_failure"
        assert "Tokyo" in refused.json()["error"]["message"]
        assert json.loads(tokyo.receive_text())["type"] == "cancel"
        assert _counts(api) == before
    finally:
        ws.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------
# Who may be asked, and what does not combine
# ---------------------------------------------------------------------------------------------


def test_a_node_that_is_offline_or_cannot_ask_dns_cannot_be_chosen(api: Api) -> None:
    api.become_admin()
    offline, _ = api.enroll_node("Offline")
    old, old_cred = api.enroll_node("Old")
    ws, _session = connect(api, old_cred, kinds=PING)  # online, but knows nothing but ping
    try:
        for node in (offline, old, "ghost"):
            refused = api.post(
                "/api/v1/measurements",
                {"target": "example.com", "dns_mode": "fixed", "resolve_node_id": node},
            )
            assert refused.status_code == 409, node
            assert refused.json()["error"]["code"] == "node_unavailable", node
    finally:
        ws.__exit__(None, None, None)


def test_a_node_waiting_for_approval_cannot_be_chosen(api: Api) -> None:
    api.become_admin()
    pending, credential = api.enroll_node("Pending", approve=False)
    ws, _session = connect(api, credential, kinds=PING_AND_DNS)
    try:
        refused = api.post(
            "/api/v1/measurements",
            {"target": "example.com", "dns_mode": "fixed", "resolve_node_id": pending},
        )
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "node_unavailable"
    finally:
        ws.__exit__(None, None, None)


def test_choosing_a_node_only_goes_with_a_fixed_address_and_a_name_to_resolve(api: Api) -> None:
    api.become_admin()
    chosen, credential = api.enroll_node("Tokyo")
    ws, session = connect(api, credential, kinds=PING_AND_DNS)
    try:
        for body in (
            {
                "target": "example.com",
                "resolve_node_id": chosen,
            },  # each node resolves: no one for the rest
            {"target": "example.com", "dns_mode": "regional", "resolve_node_id": chosen},
            {
                "target": "example.com",
                "kind": "dns",
                "dns_mode": "fixed",
                "resolve_node_id": chosen,
            },
        ):
            refused = api.post("/api/v1/measurements", body)
            assert refused.status_code == 422, body
            assert refused.json()["error"]["code"] == "invalid_option", body

        # an address needs no one to resolve it: the choice is ignored, nothing is asked of the node
        created = api.post(
            "/api/v1/measurements",
            {"target": "93.184.216.34", "dns_mode": "fixed", "resolve_node_id": chosen},
        )
        assert created.status_code == 201, created.text
        assert created.json()["resolved_by"] is None
        assert _receive(session)["target"]["ip"] == "93.184.216.34"
    finally:
        ws.__exit__(None, None, None)


def test_the_text_endpoint_takes_the_node_too(api: Api) -> None:
    api.become_admin()
    chosen, credential = api.enroll_node("Tokyo")
    key = api.post("/api/v1/admin/api-keys", {"name": "t"}).json()["key"]
    ws, tokyo = connect(api, credential, kinds=PING_AND_DNS)
    try:
        result: dict[str, Any] = {}

        def call() -> None:
            result["response"] = api.client.get(
                f"/api/v1/text?target=example.com&dns_mode=fixed&resolve_node_id={chosen}&wait=5",
                headers={"Authorization": f"Bearer {key}"},
            )

        thread = threading.Thread(target=call)
        thread.start()
        _reply(tokyo, _receive(tokyo), _answer("A", "151.101.1.69"))
        task = _receive(tokyo)
        assert task["target"]["ip"] == "151.101.1.69"
        ids = {"task_id": task["task_id"], "attempt_id": task["attempt_id"]}
        tokyo.send_text(
            json.dumps({"type": "sample", **ids, "seq": 0, "status": "ok", "rtt_us": 9_000})
        )
        tokyo.send_text(
            json.dumps(
                {"type": "task_result", **ids, "status": "completed", "tested_ip": "151.101.1.69"}
            )
        )
        barrier(tokyo)
        thread.join(15)
        assert result["response"].status_code == 200, result["response"].text
        assert "example.com resolved to 151.101.1.69 by Tokyo, DE" in result["response"].text
    finally:
        ws.__exit__(None, None, None)
