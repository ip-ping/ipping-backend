from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ipping_backend.config import Settings
from ipping_backend.main import create_app

from .fakes import fake_resolver

SETUP_TOKEN = "test-setup-token"
PASSWORD = "correct horse battery"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """SQLite by default; set IPPING_TEST_DATABASE_URL to run the same tests on PostgreSQL."""
    url = (
        os.environ.get("IPPING_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    )
    settings = Settings(
        database_url=url,
        setup_token=SETUP_TOKEN,
        cookie_secure=False,
        log_json=False,
        log_level="WARNING",
    )
    if "IPPING_TEST_DATABASE_URL" in os.environ:
        asyncio.run(_reset_database(settings.database_url))
    return settings


async def _reset_database(url: str) -> None:
    from ipping_backend.db import make_engine
    from ipping_backend.models import Base

    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


class Api:
    """Thin wrapper that carries the CSRF token the way the frontend does."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.csrf = ""

    def start_session(self) -> dict[str, Any]:
        data = self.client.get("/api/v1/session").json()
        self.csrf = data["csrf_token"]
        return data  # type: ignore[no-any-return]

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        headers = {"X-CSRF-Token": self.csrf, **kwargs.pop("headers", {})}
        return self.client.request(method, url, headers=headers, **kwargs)

    def post(self, url: str, json: Any = None, **kw: Any) -> Any:
        return self.request("POST", url, json=json, **kw)

    def get(self, url: str, **kw: Any) -> Any:
        return self.request("GET", url, **kw)

    def patch(self, url: str, json: Any = None, **kw: Any) -> Any:
        return self.request("PATCH", url, json=json, **kw)

    def become_admin(self) -> None:
        self.start_session()
        response = self.post(
            "/api/v1/setup",
            {"setup_token": SETUP_TOKEN, "username": "admin", "password": PASSWORD},
        )
        assert response.status_code == 200, response.text
        self.csrf = response.json()["csrf_token"]

    def enroll_node(
        self, name: str = "Test node", approve: bool = True, tier: str | None = None, **extra: Any
    ) -> tuple[str, str]:
        label: dict[str, Any] = {"label": name, **({"tier": tier} if tier else {})}
        token = self.post("/api/v1/admin/enrollment-tokens", label).json()["token"]
        response = self.client.post(
            "/api/v1/nodes/enroll",
            json={
                "token": token,
                "name": name,
                "version": "0.1.0",
                "country": "DE",
                "city": "Falkenstein",
                **extra,
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        if approve:
            assert self.post(f"/api/v1/admin/nodes/{body['node_id']}/approve").status_code == 200
        return body["node_id"], body["credential"]


@pytest.fixture
def app(settings: Settings):  # type: ignore[no-untyped-def]
    return create_app(settings, resolver=fake_resolver, create_schema=True)


@pytest.fixture
def client(app) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    with TestClient(app) as c:
        yield c


@pytest.fixture
def api(client: TestClient) -> Api:
    return Api(client)


def node_headers(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


def hello(ipv4: bool = True, ipv6: bool = True, kinds: tuple[str, ...] = ("ping",)) -> str:
    return json.dumps(
        {
            "type": "hello",
            "protocol_versions": [1],
            "node_version": "0.1.0",
            "capabilities": {"kinds": list(kinds), "ipv4": ipv4, "ipv6": ipv6},
        }
    )
