"""Community nodes: volunteers' nodes are left out unless asked for, never move a location estimate until their
place has been checked, are rate limited, and can have their declared place audited by verified nodes."""

from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import update

from ipping_backend.models import Node
from ipping_backend.nodes.audit import MIN_VERIFIERS, Verifier, judge

from .conftest import Api
from .test_api import connect
from .test_location_api import answer, close, fleet
from .test_probe_kinds import _start

FRANKFURT = Verifier("f", "Frankfurt", 50.11, 8.68, 9.0)
LONDON = Verifier("l", "London", 51.51, -0.13, 14.0)
AMSTERDAM = Verifier("a", "Amsterdam", 52.37, 4.90, 11.0)
BERLIN = (52.52, 13.40)
TOKYO = (35.68, 139.69)


# ---------------------------------------------------------------------------------------------
# The judgement: no database
# ---------------------------------------------------------------------------------------------


def test_a_place_the_nearby_nodes_do_not_contradict_is_consistent_but_not_proven() -> None:
    result = judge(*BERLIN, "93.184.216.34", [FRANKFURT, LONDON, AMSTERDAM])
    assert result["status"] == "consistent"
    assert "does not prove" in result["summary"]
    assert [e["node_name"] for e in result["evidence"]] == ["Frankfurt", "Amsterdam", "London"], (
        "nearest first"
    )
    assert all(e["distance_km"] < e["max_km"] for e in result["evidence"])


def test_a_node_that_is_not_where_it_says_is_caught_and_the_witness_is_named() -> None:
    result = judge(*TOKYO, "93.184.216.34", [FRANKFURT, LONDON, AMSTERDAM])
    assert result["status"] == "inconsistent"
    assert "Frankfurt" in result["summary"] and "9 ms" in result["summary"]
    assert "not where it says" in result["summary"]


def test_too_few_or_too_distant_witnesses_prove_nothing() -> None:
    few = judge(*TOKYO, "93.184.216.34", [FRANKFURT, LONDON])
    assert MIN_VERIFIERS == 3 and few["status"] == "inconclusive" and "at least 3" in few["summary"]

    far = [
        Verifier(str(i), f"Far {i}", 40.7, -74.0, 120.0) for i in range(3)
    ]  # 120 ms allows 18,000 km
    result = judge(*BERLIN, "93.184.216.34", far)
    assert result["status"] == "inconclusive" and "close enough" in result["summary"]
    assert judge(*BERLIN, "93.184.216.34", [])["status"] == "inconclusive"


# ---------------------------------------------------------------------------------------------
# Tiers and who is asked
# ---------------------------------------------------------------------------------------------


def _tiers(api: Api) -> dict[str, str]:
    return {n["name"]: n["tier"] for n in api.get("/api/v1/nodes").json()}


def test_the_token_decides_what_a_volunteer_becomes_and_an_administrator_can_change_it(
    api: Api,
) -> None:
    api.become_admin()
    official, _ = api.enroll_node("Ours")
    volunteer, _ = api.enroll_node("Theirs", tier="community")
    assert _tiers(api) == {"Ours": "official", "Theirs": "community"}
    tokens = api.get("/api/v1/admin/enrollment-tokens").json()
    assert {t["label"]: t["tier"] for t in tokens} == {"Ours": "official", "Theirs": "community"}
    admin = {n["name"]: n for n in api.get("/api/v1/admin/nodes").json()}
    assert admin["Theirs"]["tier"] == "community" and admin["Theirs"]["location_audit"] is None

    changed = api.patch(f"/api/v1/admin/nodes/{volunteer}", {"tier": "official"})
    assert changed.status_code == 200 and changed.json()["tier"] == "official"
    assert api.patch(f"/api/v1/admin/nodes/{official}", {"tier": "nobody"}).status_code == 422


def _relax_limits(api: Api) -> None:
    """These tests start several measurements that never finish: the usual limits would stop them."""
    patch = {
        "max_concurrent_per_session": 50,
        "max_concurrent_per_ip": 50,
        "creations_per_minute_per_ip": 600,
    }
    assert api.patch("/api/v1/admin/settings", patch).status_code == 200


def _online_pair(api: Api) -> tuple[Any, Any, Any, str, str]:
    api.become_admin()
    ours, our_cred = api.enroll_node("Ours")
    theirs, their_cred = api.enroll_node("Theirs", tier="community")
    ws_o, session_o = connect(api, our_cred)
    ws_c, session_c = connect(api, their_cred)
    _relax_limits(api)
    return (ws_o, session_o), (ws_c, session_c), None, ours, theirs  # type: ignore[return-value]


def test_all_nodes_means_the_official_ones_unless_volunteers_are_asked_for(api: Api) -> None:
    (ws_o, _), (ws_c, _), _, ours, theirs = _online_pair(api)
    try:
        default = _start(api, target="example.com")
        assert [t["node"]["id"] for t in default["tasks"]] == [ours]
        assert default["tasks"][0]["node"]["tier"] == "official"

        both = _start(api, target="example.com", include_community=True)
        assert {t["node"]["id"] for t in both["tasks"]} == {ours, theirs}
        assert {t["node"]["tier"] for t in both["tasks"]} == {"official", "community"}

        chosen = _start(api, target="example.com", node_ids=[theirs])
        assert [t["node"]["id"] for t in chosen["tasks"]] == [theirs], (
            "choosing a node by name is a choice"
        )
    finally:
        ws_o.__exit__(None, None, None)
        ws_c.__exit__(None, None, None)


def test_a_volunteers_node_is_asked_for_a_limited_number_of_tasks_a_minute(api: Api) -> None:
    (ws_o, _), (ws_c, _), _, ours, theirs = _online_pair(api)
    try:
        assert (
            api.patch("/api/v1/admin/settings", {"community_tasks_per_minute": 2}).status_code
            == 200
        )
        for _ in range(2):
            _start(api, target="example.com", include_community=True)
        # at its limit: "all nodes" quietly leaves it out, and a request that names it is told why
        third = _start(api, target="example.com", include_community=True)
        assert [t["node"]["id"] for t in third["tasks"]] == [ours]
        named = api.post("/api/v1/measurements", {"target": "example.com", "node_ids": [theirs]})
        assert named.status_code == 429 and named.json()["error"]["code"] == "node_busy"
        # an official node has no such limit
        assert _start(api, target="example.com")["tasks"][0]["node"]["id"] == ours
    finally:
        ws_o.__exit__(None, None, None)
        ws_c.__exit__(None, None, None)


def test_a_volunteers_unverified_place_cannot_move_a_location_estimate(api: Api) -> None:
    nodes = fleet(
        api,
        [
            ("Frankfurt", 50.11, 8.68, 6.5),
            ("London", 51.51, -0.13, 8.0),
            ("Paris", 48.86, 2.35, 9.0),
        ],
        verified=True,
    )
    # a volunteer in Tokyo (declared) that "hears" the target in 0.5 ms: a lie that would drag the estimate there
    _, liar_cred = api.enroll_node("Liar", tier="community", latitude=35.68, longitude=139.69)
    ws, session = connect(api, liar_cred)
    try:
        mid = api.post(
            "/api/v1/measurements",
            {"target": "example.com", "count": 4, "include_community": True, "dns_mode": "fixed"},
        ).json()["id"]
        for _, node_session, rtt in nodes:
            answer(node_session, rtt)
        answer(session, 0.5)
        estimate = api.get(f"/api/v1/measurements/{mid}/location").json()
        names = [e["node_name"] for e in estimate["evidence"]]
        assert "Liar" not in names and len(names) == 3, names
        assert estimate["status"] == "estimated" and estimate["likely"]["country"] != "JP"
    finally:
        ws.__exit__(None, None, None)
        close(nodes)


# ---------------------------------------------------------------------------------------------
# The audit through the API
# ---------------------------------------------------------------------------------------------


def _setup_audit(api: Api, declared: tuple[float, float]) -> tuple[Any, str, list[Any]]:
    sites = [
        ("Frankfurt", 50.11, 8.68, 9.0),
        ("London", 51.51, -0.13, 14.0),
        ("Amsterdam", 52.37, 4.90, 11.0),
    ]
    verifiers = fleet(api, sites, verified=True)
    _relax_limits(api)
    volunteer, _ = api.enroll_node(
        "Volunteer", tier="community", latitude=declared[0], longitude=declared[1]
    )

    async def give_public_address() -> None:
        async with api.client.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
            await db.execute(
                update(Node).where(Node.id == volunteer).values(remote_addr="93.184.216.34")
            )
            await db.commit()

    api.client.portal.call(give_public_address)
    return None, volunteer, verifiers


def _audit(api: Api, volunteer: str, verifiers: list[Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}

    def call() -> None:
        try:
            result["response"] = api.post(f"/api/v1/admin/nodes/{volunteer}/audit-location")
        except Exception as exc:  # noqa: BLE001 - shown by the test that started the thread
            result["error"] = exc

    thread = threading.Thread(target=call)
    thread.start()
    for _, session, rtt in verifiers:
        answer(session, rtt, count=3)
    thread.join(40)
    if "error" in result:
        raise result["error"]
    return result["response"]  # type: ignore[no-any-return]


def test_a_declared_place_the_verified_nodes_do_not_contradict_is_consistent(api: Api) -> None:
    _, volunteer, verifiers = _setup_audit(api, (52.52, 13.40))
    try:
        response = _audit(api, volunteer, verifiers)
        assert response.status_code == 200, response.text
        audit = response.json()["location_audit"]
        assert audit["status"] == "consistent" and audit["address"] == "93.184.216.34"
        assert len(audit["evidence"]) == 3
        # it only reports: nothing was verified on its own
        assert response.json()["location_verified"] is False
        # and it is remembered
        node = {n["id"]: n for n in api.get("/api/v1/admin/nodes").json()}[volunteer]
        assert node["location_audit"]["status"] == "consistent"
    finally:
        close(verifiers)


def test_a_declared_place_the_verified_nodes_rule_out_is_inconsistent(api: Api) -> None:
    _, volunteer, verifiers = _setup_audit(api, (35.68, 139.69))
    try:
        audit = _audit(api, volunteer, verifiers).json()["location_audit"]
        assert audit["status"] == "inconsistent" and "not where it says" in audit["summary"]
    finally:
        close(verifiers)


def test_a_node_that_connects_from_a_private_address_cannot_be_audited(api: Api) -> None:
    api.become_admin()
    volunteer, _ = api.enroll_node("Home", tier="community", latitude=52.52, longitude=13.40)
    response = api.post(f"/api/v1/admin/nodes/{volunteer}/audit-location")
    assert response.status_code == 200, response.text
    audit = response.json()["location_audit"]
    assert audit["status"] == "inconclusive" and "not public" in audit["summary"]
    # no declared place, nothing to check
    bare, _ = api.enroll_node("Bare", tier="community", latitude=None, longitude=None)
    refused = api.post(f"/api/v1/admin/nodes/{bare}/audit-location")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "no_location"


def test_the_audit_is_for_administrators(api: Api) -> None:
    api.become_admin()
    volunteer, _ = api.enroll_node("Home", tier="community", latitude=52.52, longitude=13.40)
    api.client.cookies.clear()
    stranger = Api(api.client)
    stranger.start_session()
    assert stranger.post(f"/api/v1/admin/nodes/{volunteer}/audit-location").status_code in {
        401,
        403,
    }
