"""Alembic environment: async engine, so the same URL works for the app and for migrations."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from ipping_backend.models import Base

config = context.config
target_metadata = Base.metadata


def _configure(connection: Connection | None, url: str | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=True,  # SQLite cannot ALTER most things in place
        literal_binds=connection is None,
    )


def run_migrations_offline() -> None:
    _configure(None, config.get_main_option("sqlalchemy.url"))
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("No database URL configured for the migration")
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
