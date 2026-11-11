"""Enrollment keys that can be used again, and a revoked node that is gone for good."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select, update

from ipping_backend import maintenance
from ipping_backend.models import EnrollmentToken, Node

from .conftest import Api, node_headers

TOKENS = "/api/v1/admin/enrollment-tokens"
ENROLL = "/api/v1/nodes/enroll"


def _enroll(api: Api, token: str, name: str) -> Any:
    return api.client.post(ENROLL, json={"token": token, "name": name, "version": "0.1.0"})


def _token(api: Api, **body: Any) -> tuple[str, str]:
    created = api.post(TOKENS, body).json()
    return created["id"], created["token"]


def _listed(api: Api, token_id: str) -> dict[str, Any]:
    return next(t for t in api.get(TOKENS).json() if t["id"] == token_id)


# ---------------------------------------------------------------------------------------------
# A key that can be used again
# ---------------------------------------------------------------------------------------------


def test_a_single_use_key_is_still_the_default_and_is_taken_by_the_first_node(api: Api) -> None:
    api.become_admin()
    token_id, token = _token(api, label="once")
    assert _listed(api, token_id)["reusable"] is False
    assert _enroll(api, token, "first").status_code == 201
    assert _enroll(api, token, "second").status_code == 403
    listed = _listed(api, token_id)
    assert listed["state"] == "used" and listed["uses"] == 1


def test_a_reusable_key_enrolls_any_number_of_nodes_and_each_waits_for_approval(api: Api) -> None:
    api.become_admin()
    token_id, token = _token(api, label="many", reusable=True)
    ids = []
    for name in ("a", "b", "c"):
        response = _enroll(api, token, name)
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "pending"
        ids.append(response.json()["node_id"])
    assert len(set(ids)) == 3
    listed = _listed(api, token_id)
    assert listed["reusable"] is True and listed["state"] == "active"
    assert listed["uses"] == 3 and listed["node_id"] == ids[-1]
    assert api.get("/api/v1/nodes").json() == []  # still none of them is public
    assert {n["id"] for n in api.get("/api/v1/admin/nodes").json()} == set(ids)


def test_a_reusable_key_stops_when_it_is_revoked_or_expires(api: Api) -> None:
    api.become_admin()
    token_id, token = _token(api, reusable=True)
    assert _enroll(api, token, "a").status_code == 201
    assert api.request("DELETE", f"{TOKENS}/{token_id}").status_code == 204
    assert _enroll(api, token, "b").status_code == 403
    assert _listed(api, token_id)["state"] == "revoked"

    other_id, other = _token(api, reusable=True)

    async def expire() -> None:
        from datetime import timedelta

        from ipping_backend.models import utcnow

        async with api.client.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
            await db.execute(
                update(EnrollmentToken)
                .where(EnrollmentToken.id == other_id)
                .values(expires_at=utcnow() - timedelta(minutes=1))
            )
            await db.commit()

    api.client.portal.call(expire)
    assert _enroll(api, other, "late").status_code == 403
    assert _listed(api, other_id)["state"] == "expired"


def test_a_key_can_be_good_for_up_to_thirty_days(api: Api) -> None:
    api.become_admin()
    assert api.post(TOKENS, {"ttl_minutes": 30 * 24 * 60, "reusable": True}).status_code == 201
    assert api.post(TOKENS, {"ttl_minutes": 30 * 24 * 60 + 1}).status_code == 422


def test_a_reusable_community_key_makes_community_nodes(api: Api) -> None:
    api.become_admin()
    _, token = _token(api, tier="community", reusable=True)
    ids = [_enroll(api, token, name).json()["node_id"] for name in ("a", "b")]
    tiers = {n["id"]: n["tier"] for n in api.get("/api/v1/admin/nodes").json()}
    assert [tiers[i] for i in ids] == ["community", "community"]


# ---------------------------------------------------------------------------------------------
# A revoked node does not come back
# ---------------------------------------------------------------------------------------------


def test_a_revoked_node_is_gone_from_the_lists_and_its_key_is_refused(api: Api) -> None:
    from starlette.websockets import WebSocketDisconnect

    api.become_admin()
    keep, _ = api.enroll_node("keep")
    gone, credential = api.enroll_node("gone")
    assert {n["id"] for n in api.get("/api/v1/admin/nodes").json()} == {keep, gone}

    revoked = api.post(f"/api/v1/admin/nodes/{gone}/revoke")
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    assert revoked.json()["name"] == "gone"  # what was removed is said once

    assert [n["id"] for n in api.get("/api/v1/admin/nodes").json()] == [keep]
    assert [n["id"] for n in api.get("/api/v1/nodes").json()] == [keep]
    # it cannot be found, approved or edited any more, and its credential no longer opens a connection
    assert api.post(f"/api/v1/admin/nodes/{gone}/approve").status_code == 404
    assert api.patch(f"/api/v1/admin/nodes/{gone}", {"name": "back"}).status_code == 404
    with (
        pytest.raises(WebSocketDisconnect),
        api.client.websocket_connect("/api/v1/nodes/connect", headers=node_headers(credential)),
    ):
        pass
    audit = [e for e in api.get("/api/v1/admin/audit").json() if e["action"] == "node.revoked"]
    assert audit and audit[0]["subject"] == gone


def test_a_node_revoked_before_this_change_is_hidden_and_then_swept_away(api: Api) -> None:
    api.become_admin()
    old, _ = api.enroll_node("old")
    sm = api.client.app.state.sessionmaker  # type: ignore[attr-defined]

    async def mark_revoked() -> None:
        async with sm() as db:
            await db.execute(update(Node).where(Node.id == old).values(status="revoked"))
            await db.commit()

    async def count() -> int:
        async with sm() as db:
            return len((await db.execute(select(Node.id).where(Node.id == old))).all())

    api.client.portal.call(mark_revoked)
    assert api.get("/api/v1/admin/nodes").json() == []  # not listed, though the row is still there
    assert api.client.portal.call(count) == 1
    removed = api.client.portal.call(maintenance.run_once, sm)
    assert removed["revoked_nodes"] == 1 and api.client.portal.call(count) == 0


def test_there_is_no_way_to_rotate_a_nodes_key_any_more(api: Api) -> None:
    api.become_admin()
    node, credential = api.enroll_node()
    assert api.post(f"/api/v1/admin/nodes/{node}/rotate-credential").status_code in (404, 405)
    # and the key still works
    assert credential.startswith(node)
