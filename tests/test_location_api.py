from __future__ import annotations

import json
from typing import Any

from .conftest import Api
from .test_api import barrier, connect

# Frankfurt, London and New York: enough for an estimate, with a target "near Amsterdam".
SITES = [
    ("Frankfurt", 50.11, 8.68, 6.5),
    ("London", 51.51, -0.13, 8.0),
    ("New York", 40.71, -74.0, 85.0),
]

Fleet = list[tuple[Any, Any, float]]


def answer(session: Any, rtt_ms: float, count: int = 4) -> None:
    task = json.loads(session.receive_text())
    ids = {"task_id": task["task_id"], "attempt_id": task["attempt_id"]}
    session.send_text(json.dumps({"type": "task_ack", **ids}))
    for seq in range(count):
        sample = {
            "type": "sample",
            **ids,
            "seq": seq,
            "status": "ok",
            "rtt_us": int((rtt_ms + seq * 0.2) * 1000),
        }
        session.send_text(json.dumps(sample))
    result = {
        "type": "task_result",
        **ids,
        "status": "completed",
        "tested_ip": task["target"].get("ip"),
    }
    session.send_text(json.dumps(result))
    barrier(session)


def fleet(api: Api, sites: list[tuple[str, float, float, float]], verified: bool = False) -> Fleet:
    api.become_admin()
    nodes = []
    for name, lat, lon, rtt in sites:
        node_id, credential = api.enroll_node(name, latitude=lat, longitude=lon)
        if verified:
            api.patch(f"/api/v1/admin/nodes/{node_id}", {"location_verified": True})
        ws, session = connect(api, credential)
        nodes.append((ws, session, rtt))
    return nodes


def close(nodes: Fleet) -> None:
    for ws, _, _ in nodes:
        ws.__exit__(None, None, None)


def test_location_end_to_end_with_caching_and_states(api: Api) -> None:
    nodes = fleet(api, SITES)
    try:
        created = api.post("/api/v1/measurements", {"target": "example.com", "count": 4}).json()
        mid = created["id"]

        # While the nodes are still working there is nothing to estimate yet.
        pending = api.get(f"/api/v1/measurements/{mid}/location").json()
        assert pending["status"] == "pending"

        for _, session, rtt in nodes:
            answer(session, rtt)
        assert api.get(f"/api/v1/measurements/{mid}").json()["status"] == "completed"

        first = api.get(f"/api/v1/measurements/{mid}/location").json()
        assert first["status"] == "estimated", first["summary"]
        assert first["target_ip"] == "93.184.216.34"
        assert first["likely"]["country"] in {"NL", "DE", "BE", "GB", "FR", "LU"}
        assert first["closest"]["node_name"] == "Frankfurt"
        assert first["closest"]["max_distance_km"] == round(150 * first["closest"]["rtt_ms"])
        assert [e["node_name"] for e in first["evidence"]] == ["Frankfurt", "London", "New York"]
        assert all(e["used"] and not e["verified"] for e in first["evidence"])
        assert any("not been verified" in w for w in first["warnings"])
        assert first["strength"] in {"low", "moderate"}
        assert any("anycast" in t.lower() for t in first["limitations"])

        # Stored once: the second answer is the same document, not a new computation.
        assert api.get(f"/api/v1/measurements/{mid}/location").json() == first
    finally:
        close(nodes)


def test_verified_coordinates_are_recorded_with_the_measurement(api: Api) -> None:
    nodes = fleet(api, SITES, verified=True)
    try:
        mid = api.post("/api/v1/measurements", {"target": "example.com"}).json()["id"]
        for _, session, rtt in nodes:
            answer(session, rtt)
        result = api.get(f"/api/v1/measurements/{mid}/location").json()
        assert all(e["verified"] for e in result["evidence"])
        assert not any("not been verified" in w for w in result["warnings"])
    finally:
        close(nodes)


def test_two_nodes_are_not_enough(api: Api) -> None:
    nodes = fleet(api, SITES[:2])
    try:
        mid = api.post("/api/v1/measurements", {"target": "example.com"}).json()["id"]
        for _, session, rtt in nodes:
            answer(session, rtt)
        result = api.get(f"/api/v1/measurements/{mid}/location").json()
        assert result["status"] == "insufficient_evidence" and result["likely"] is None
        assert "at least 3" in result["summary"]
    finally:
        close(nodes)


def test_nodes_without_coordinates_are_not_evidence(api: Api) -> None:
    api.become_admin()
    sessions = []
    for name in ("a", "b", "c"):
        _, credential = api.enroll_node(name)  # no latitude/longitude
        sessions.append(connect(api, credential))
    try:
        mid = api.post("/api/v1/measurements", {"target": "example.com"}).json()["id"]
        for _, session in sessions:
            answer(session, 5.0)
        result = api.get(f"/api/v1/measurements/{mid}/location").json()
        assert result["status"] == "insufficient_evidence" and result["evidence"] == []
    finally:
        for ws, _ in sessions:
            ws.__exit__(None, None, None)


def test_contradictory_nodes_give_no_single_place(api: Api) -> None:
    far_apart = [
        ("Frankfurt", 50.11, 8.68, 2.0),
        ("Singapore", 1.35, 103.82, 2.0),
        ("Sydney", -33.87, 151.21, 2.0),
    ]
    nodes = fleet(api, far_apart)
    try:
        mid = api.post("/api/v1/measurements", {"target": "example.com"}).json()["id"]
        for _, session, rtt in nodes:
            answer(session, rtt)
        result = api.get(f"/api/v1/measurements/{mid}/location").json()
        assert result["status"] == "inconclusive" and result["likely"] is None
        assert "Possible explanations include anycast" in result["summary"]
    finally:
        close(nodes)


def test_regional_dns_measurements_are_not_estimated(api: Api) -> None:
    nodes = fleet(api, SITES)
    try:
        body = {"target": "example.com", "dns_mode": "regional"}
        mid = api.post("/api/v1/measurements", body).json()["id"]
        for _, session, rtt in nodes:
            task = json.loads(session.receive_text())
            ids = {"task_id": task["task_id"], "attempt_id": task["attempt_id"]}
            sample = {"type": "sample", **ids, "seq": 0, "status": "ok", "rtt_us": int(rtt * 1000)}
            session.send_text(json.dumps(sample))
            result = {
                "type": "task_result",
                **ids,
                "status": "completed",
                "tested_ip": "93.184.216.34",
            }
            session.send_text(json.dumps(result))
            barrier(session)
        located = api.get(f"/api/v1/measurements/{mid}/location").json()
        assert located["status"] == "unavailable" and "Same IP for every node" in located["summary"]
    finally:
        close(nodes)


def test_location_follows_the_same_access_rules_as_the_measurement(api: Api) -> None:
    nodes = fleet(api, SITES)
    try:
        mid = api.post("/api/v1/measurements", {"target": "example.com"}).json()["id"]
        for _, session, rtt in nodes:
            answer(session, rtt)
        owner = dict(api.client.cookies)

        api.client.cookies.clear()
        stranger = Api(api.client)
        stranger.start_session()
        assert stranger.get(f"/api/v1/measurements/{mid}/location").status_code == 404

        api.client.cookies.clear()
        api.client.cookies.update(owner)
        api.post(f"/api/v1/measurements/{mid}/share", {"shared": True})
        api.client.cookies.clear()
        stranger.start_session()
        assert stranger.get(f"/api/v1/measurements/{mid}/location").json()["status"] == "estimated"
    finally:
        close(nodes)
