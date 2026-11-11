"""A node with no name is called after its place and its network."""

from __future__ import annotations

import json
from typing import Any

from ipping_backend.nodes.naming import node_label

from .conftest import Api
from .test_api import barrier, connect

ENROLL = "/api/v1/nodes/enroll"


def _label(**over: Any) -> str:
    fields: dict[str, Any] = {
        "name": "",
        "city": "Sydney",
        "region": "New South Wales",
        "country": "AU",
        "provider": "OVH SAS",
        "asn": 16276,
        "node_id": "abcdef123456",
    }
    return node_label(**{**fields, **over})


def test_the_default_name_is_the_place_and_the_network() -> None:
    assert _label() == "Sydney, AU - OVH SAS · AS16276"
    # the place falls back to the region, then the country alone
    assert _label(city=None) == "New South Wales, AU - OVH SAS · AS16276"
    assert _label(city=None, region=None) == "AU - OVH SAS · AS16276"
    # either half of the network may be missing, and either part of the whole
    assert _label(asn=None) == "Sydney, AU - OVH SAS"
    assert _label(provider=None) == "Sydney, AU - AS16276"
    assert _label(city=None, region=None, country=None) == "OVH SAS · AS16276"
    assert _label(provider=None, asn=None) == "Sydney, AU"
    # nothing known at all: something to tell it apart by
    assert _label(city=None, region=None, country=None, provider=None, asn=None) == "Node abcdef"


def test_a_name_somebody_gave_is_kept() -> None:
    assert _label(name="Falkenstein 1") == "Falkenstein 1"
    assert _label(name="   ") == "Sydney, AU - OVH SAS · AS16276"  # blank is no name
    assert _label(name="  padded ") == "padded"


def _enroll_with_key(api: Api, **details: Any) -> tuple[str, str]:
    token = api.post("/api/v1/admin/enrollment-tokens", {}).json()["token"]
    response = api.client.post(ENROLL, json={"token": token, "version": "0.1.0", **details})
    assert response.status_code == 201, response.text
    return response.json()["node_id"], response.json()["credential"]


def _enroll(api: Api, **details: Any) -> str:
    return _enroll_with_key(api, **details)[0]


def _admin_node(api: Api, node_id: str) -> dict[str, Any]:
    return next(n for n in api.get("/api/v1/admin/nodes").json() if n["id"] == node_id)


def test_a_node_can_enroll_without_a_name_and_is_called_after_where_it_is(api: Api) -> None:
    api.become_admin()
    for details in ({}, {"name": ""}, {"name": "   "}):
        node_id = _enroll(
            api, country="au", city="Sydney", provider="OVH SAS", asn=16276, **details
        )
        node = _admin_node(api, node_id)
        assert node["name"] == "Sydney, AU - OVH SAS · AS16276", details
        assert node["custom_name"] == ""
    api.post(f"/api/v1/admin/nodes/{node_id}/approve")
    assert [n["name"] for n in api.get("/api/v1/nodes").json()] == [
        "Sydney, AU - OVH SAS · AS16276"
    ]


def test_a_node_that_enrolled_with_a_name_keeps_it_until_the_name_is_cleared(api: Api) -> None:
    api.become_admin()
    node_id = _enroll(
        api, name="Falkenstein 1", country="DE", city="Falkenstein", provider="Hetzner"
    )
    node = _admin_node(api, node_id)
    assert node["name"] == "Falkenstein 1" and node["custom_name"] == "Falkenstein 1"
    cleared = api.patch(f"/api/v1/admin/nodes/{node_id}", {"name": ""})
    assert cleared.status_code == 200
    assert (
        cleared.json()["name"] == "Falkenstein, DE - Hetzner"
        and cleared.json()["custom_name"] == ""
    )
    named = api.patch(f"/api/v1/admin/nodes/{node_id}", {"name": "  Back  "})
    assert named.json()["name"] == "Back"


def test_the_default_name_follows_the_data_and_a_measurement_carries_it(api: Api) -> None:
    api.become_admin()
    node_id, credential = _enroll_with_key(api, country="DE", city="Falkenstein")
    api.post(f"/api/v1/admin/nodes/{node_id}/approve")
    assert _admin_node(api, node_id)["name"] == "Falkenstein, DE"
    # the provider and the AS number arrive later (from the IP data, or typed): the name follows
    patched = api.patch(f"/api/v1/admin/nodes/{node_id}", {"provider": "Hetzner", "asn": 24940})
    assert patched.status_code == 200
    assert _admin_node(api, node_id)["name"] == "Falkenstein, DE - Hetzner · AS24940"

    ws, session = connect(api, credential)
    try:
        measurement = api.post("/api/v1/measurements", {"target": "example.com"}).json()
        task = json.loads(session.receive_text())
        ids = {"task_id": task["task_id"], "attempt_id": task["attempt_id"]}
        session.send_text(json.dumps({"type": "task_result", **ids, "status": "failed"}))
        barrier(session)
        shown = api.get(f"/api/v1/measurements/{measurement['id']}").json()
        assert shown["tasks"][0]["node"]["name"] == "Falkenstein, DE - Hetzner · AS24940"
    finally:
        ws.__exit__(None, None, None)
