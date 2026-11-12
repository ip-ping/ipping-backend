"""Sponsored nodes: a flag that makes the node's provider's name stand out in the interface, and changes nothing else.

The token decides what a node enrolls as (as it does for the tier) and an administrator can change it; a result keeps what
it was when the measurement began.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from alembic import command

from ipping_backend.measurements.schemas import NodeInfo
from ipping_backend.migrate import alembic_config

from .conftest import Api
from .test_api import connect
from .test_probe_kinds import _start


def _public(api: Api) -> dict[str, dict[str, Any]]:
    return {n["name"]: n for n in api.get("/api/v1/nodes").json()}


def _admin(api: Api) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in api.get("/api/v1/admin/nodes").json()}


def test_nothing_is_sponsored_unless_it_is_made_so(api: Api) -> None:
    api.become_admin()
    node, _ = api.enroll_node("Plain", provider="Hetzner Online GmbH")
    assert _public(api)["Plain"]["sponsored"] is False
    assert _admin(api)[node]["sponsored"] is False
    assert [t["sponsored"] for t in api.get("/api/v1/admin/enrollment-tokens").json()] == [False]


def test_the_token_decides_whether_a_node_is_sponsored_and_an_administrator_can_change_it(
    api: Api,
) -> None:
    api.become_admin()
    plain, _ = api.enroll_node("Plain")
    backed, _ = api.enroll_node("Backed", sponsored=True)
    assert {n["name"]: n["sponsored"] for n in _public(api).values()} == {
        "Plain": False,
        "Backed": True,
    }
    tokens = api.get("/api/v1/admin/enrollment-tokens").json()
    assert {t["label"]: t["sponsored"] for t in tokens} == {"Plain": False, "Backed": True}
    assert _admin(api)[backed]["sponsored"] is True

    on = api.patch(f"/api/v1/admin/nodes/{plain}", {"sponsored": True})
    assert on.status_code == 200 and on.json()["sponsored"] is True
    assert _public(api)["Plain"]["sponsored"] is True
    off = api.patch(f"/api/v1/admin/nodes/{backed}", {"sponsored": False})
    assert off.status_code == 200 and off.json()["sponsored"] is False
    assert _public(api)["Backed"]["sponsored"] is False

    # the change is on the audit trail, with the field it changed
    events = api.get("/api/v1/admin/audit").json()
    changed = [e for e in events if e["action"] == "node.updated"]
    assert changed and all("sponsored" in e["data"]["fields"] for e in changed)


def test_a_flag_left_out_or_sent_as_null_is_left_as_it_is(api: Api) -> None:
    api.become_admin()
    node, _ = api.enroll_node("Backed", sponsored=True)
    # another field changes: the flag stays
    assert (
        api.patch(f"/api/v1/admin/nodes/{node}", {"city": "Nuremberg"}).json()["sponsored"] is True
    )
    # null is not "off". It was an unhandled database error for the two flags that were there before this one (tier and
    # location_verified), so all three are checked.
    nulled = api.patch(
        f"/api/v1/admin/nodes/{node}",
        {"sponsored": None, "tier": None, "location_verified": None},
    )
    assert nulled.status_code == 200
    assert nulled.json()["sponsored"] is True
    assert nulled.json()["tier"] == "official" and nulled.json()["location_verified"] is False
    assert api.patch(f"/api/v1/admin/nodes/{node}", {"sponsored": "maybe"}).status_code == 422


def test_sponsored_has_nothing_to_do_with_who_runs_the_node(api: Api) -> None:
    """A volunteer's node can be sponsored, and a sponsored node is still official unless it is made community."""
    api.become_admin()
    both, _ = api.enroll_node("Both", tier="community", sponsored=True)
    ours, _ = api.enroll_node("Ours", sponsored=True)
    assert (_admin(api)[both]["tier"], _admin(api)[both]["sponsored"]) == ("community", True)
    assert (_admin(api)[ours]["tier"], _admin(api)[ours]["sponsored"]) == ("official", True)
    api.patch(f"/api/v1/admin/nodes/{both}", {"tier": "official"})
    assert _admin(api)[both]["sponsored"] is True, "changing the tier leaves it"
    api.patch(f"/api/v1/admin/nodes/{ours}", {"sponsored": False})
    assert _admin(api)[ours]["tier"] == "official"


def test_a_result_keeps_what_the_node_was_when_the_measurement_began(api: Api) -> None:
    api.become_admin()
    node, credential = api.enroll_node("Backed", sponsored=True, provider="Hetzner Online GmbH")
    ws, _session = connect(api, credential)
    try:
        first = _start(api, target="example.com")
        assert [t["node"]["sponsored"] for t in first["tasks"]] == [True]

        api.patch(f"/api/v1/admin/nodes/{node}", {"sponsored": False})
        again = api.get(f"/api/v1/measurements/{first['id']}").json()
        assert [t["node"]["sponsored"] for t in again["tasks"]] == [True], "an old result stays"
        assert [t["node"]["provider"] for t in again["tasks"]] == ["Hetzner Online GmbH"]

        later = _start(api, target="example.com")
        assert [t["node"]["sponsored"] for t in later["tasks"]] == [False]
    finally:
        ws.__exit__(None, None, None)


def test_a_result_stored_before_the_flag_existed_is_not_sponsored() -> None:
    assert NodeInfo.model_validate({"id": "n1", "name": "Old"}).sponsored is False


def test_the_columns_are_added_to_a_database_that_has_nodes_and_tokens(tmp_path: Path) -> None:
    """0014 adds `sponsored` with everything already there not sponsored."""
    path = tmp_path / "s.db"
    url = f"sqlite+aiosqlite:///{path}"
    command.upgrade(alembic_config(url), "0013")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "insert into nodes (id, name, status, credential_hash, auto_fields, location_verified,"
            " tier, capabilities, created_at) values ('n1', 'Old', 'approved', 'h', '[]', 0,"
            " 'community', '{}', '2026-01-01 00:00:00')"
        )
        conn.execute(
            "insert into enrollment_tokens (id, token_hash, created_by, created_at, expires_at,"
            " tier, reusable, uses) values ('t1', 'h', 'admin', '2026-01-01 00:00:00',"
            " '2026-01-02 00:00:00', 'official', 0, 0)"
        )
    command.upgrade(alembic_config(url), "head")
    with sqlite3.connect(path) as conn:
        assert conn.execute("select sponsored, tier from nodes").fetchall() == [(0, "community")]
        assert conn.execute("select sponsored from enrollment_tokens").fetchall() == [(0,)]
    command.downgrade(alembic_config(url), "0013")
    with sqlite3.connect(path) as conn:
        columns = [row[1] for row in conn.execute("pragma table_info(nodes)")]
        assert "sponsored" not in columns and "tier" in columns
