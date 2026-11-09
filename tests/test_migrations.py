from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from ipping_backend.migrate import alembic_config, ensure_schema_is_current, head_revision
from ipping_backend.models import Base


def test_migrations_build_exactly_the_schema_the_models_describe(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'm.db'}"
    command.upgrade(alembic_config(url), "head")

    async def drift() -> list[object]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                await ensure_schema_is_current(engine)
                return await conn.run_sync(
                    lambda sync: compare_metadata(
                        MigrationContext.configure(sync, opts={"compare_type": True}), Base.metadata
                    )
                )
        finally:
            await engine.dispose()

    assert asyncio.run(drift()) == []


def test_startup_refuses_an_unmigrated_database(tmp_path: Path) -> None:
    import pytest

    async def check() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
        try:
            await ensure_schema_is_current(engine)
        finally:
            await engine.dispose()

    with pytest.raises(RuntimeError, match="ipping-backend migrate"):
        asyncio.run(check())
    assert head_revision() == "0010"


def test_dns_is_enabled_for_deployments_that_never_narrowed_the_kinds(tmp_path: Path) -> None:
    """0006 adds dns to a saved "everything on" list, and leaves a deliberate restriction alone."""
    import json
    import sqlite3

    def kinds_after_upgrade(saved: list[str] | None) -> list[str] | None:
        path = tmp_path / f"k{len(saved or [])}{'n' if saved is None else 's'}.db"
        url = f"sqlite+aiosqlite:///{path}"
        command.upgrade(alembic_config(url), "0005")
        with sqlite3.connect(path) as conn:
            if saved is not None:
                conn.execute(
                    "insert into app_settings (key, value) values ('enabled_kinds', ?)",
                    (json.dumps(saved),),
                )
        command.upgrade(alembic_config(url), "0006")
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "select value from app_settings where key='enabled_kinds'"
            ).fetchone()
        return None if row is None else json.loads(row[0])  # type: ignore[no-any-return]

    everything = ["ping", "tcp", "http", "traceroute"]
    assert kinds_after_upgrade(everything) == [*everything, "dns"]
    assert kinds_after_upgrade(["ping", "tcp"]) == ["ping", "tcp"]
    assert kinds_after_upgrade(None) is None


def test_tls_is_enabled_for_deployments_that_never_narrowed_the_kinds(tmp_path: Path) -> None:
    """0009 adds tls to a saved "everything on" list, and leaves a deliberate restriction alone."""
    import json
    import sqlite3

    def kinds_after_upgrade(saved: list[str] | None) -> list[str] | None:
        path = tmp_path / f"t{len(saved or [])}{'n' if saved is None else 's'}.db"
        url = f"sqlite+aiosqlite:///{path}"
        command.upgrade(alembic_config(url), "0008")
        with sqlite3.connect(path) as conn:
            if saved is not None:
                conn.execute(
                    "insert into app_settings (key, value) values ('enabled_kinds', ?)",
                    (json.dumps(saved),),
                )
        command.upgrade(alembic_config(url), "0009")
        with sqlite3.connect(path) as conn:
            row = conn.execute(
                "select value from app_settings where key='enabled_kinds'"
            ).fetchone()
        return None if row is None else json.loads(row[0])  # type: ignore[no-any-return]

    everything = ["ping", "tcp", "http", "traceroute", "dns"]
    assert kinds_after_upgrade(everything) == [*everything, "tls"]
    assert kinds_after_upgrade(["ping", "tcp"]) == ["ping", "tcp"]
    assert kinds_after_upgrade(None) is None
