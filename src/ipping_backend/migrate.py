"""Alembic helpers. Migrations ship inside the package so an installed wheel can run them."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(database_url: str | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if database_url:
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


async def ensure_schema_is_current(engine: AsyncEngine) -> None:
    """Fail fast, with the fix in the message, if the migrate step was skipped."""

    def current(sync_conn) -> str | None:  # type: ignore[no-untyped-def]
        return MigrationContext.configure(sync_conn).get_current_revision()

    async with engine.connect() as conn:
        found = await conn.run_sync(current)
    expected = head_revision()
    if found != expected:
        raise RuntimeError(
            f"Database schema is at {found or 'nothing'} but this version needs {expected}. "
            "Run `ipping-backend migrate` first."
        )
