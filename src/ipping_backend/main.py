from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import func, select, text

from . import __version__, maintenance
from .admin import auth as admin_auth
from .admin import router as admin_router
from .config import Settings, get_settings
from .db import make_engine, make_sessionmaker
from .errors import install_error_handlers
from .logging_config import configure_logging
from .measurements import router as measurements_router
from .measurements.engine import MeasurementEngine
from .measurements.events import EventBus
from .models import Base, User
from .nodes import router as nodes_router
from .nodes.registry import NodeRegistry
from .security.ratelimit import Limiters
from .security.targets import Resolver, system_resolver

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    resolver: Resolver = system_resolver,
    create_schema: bool = False,
) -> FastAPI:
    """Build the application.

    `create_schema` creates tables directly instead of requiring `ipping-backend migrate`.
    It exists for tests; deployments always migrate.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, settings.log_json)
        db_engine = make_engine(settings.database_url)
        sessionmaker = make_sessionmaker(db_engine)
        registry = NodeRegistry()
        bus = EventBus()
        app.state.settings = settings
        app.state.db_engine = db_engine
        app.state.sessionmaker = sessionmaker
        app.state.registry = registry
        app.state.bus = bus
        app.state.limiters = Limiters()
        app.state.engine = MeasurementEngine(sessionmaker, registry, bus, resolver)

        if create_schema:
            async with db_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        else:
            from .migrate import ensure_schema_is_current

            await ensure_schema_is_current(db_engine)

        async with sessionmaker() as db:
            has_admin = (await db.scalar(select(func.count()).select_from(User))) != 0
        app.state.setup_token = None
        if not has_admin:
            app.state.setup_token = settings.setup_token or secrets.token_urlsafe(24)
            if not settings.setup_token:
                log.warning(
                    "No administrator exists yet. Open /setup and enter this one-time setup token: %s",
                    app.state.setup_token,
                )

        await app.state.engine.recover_on_startup()
        sweeper = asyncio.create_task(maintenance.loop(sessionmaker))
        try:
            yield
        finally:
            sweeper.cancel()
            await app.state.engine.shutdown()
            await db_engine.dispose()

    app = FastAPI(
        title="ipping backend",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    install_error_handlers(app)
    app.include_router(admin_auth.router)
    app.include_router(admin_router.router)
    app.include_router(nodes_router.router)
    app.include_router(measurements_router.router)

    @app.get("/api/v1/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/health/ready", tags=["health"])
    async def ready() -> dict[str, str]:
        async with app.state.sessionmaker() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ready"}

    return app
