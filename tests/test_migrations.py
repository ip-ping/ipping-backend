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
    assert head_revision() == "0004"
