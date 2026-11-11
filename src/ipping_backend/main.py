from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from . import __version__, maintenance
from .admin import api_keys as api_keys_router
from .admin import auth as admin_auth
from .admin import router as admin_router
from .admin.auth import SETUP_TOKEN_KEY
from .cluster import Cluster
from .config import Settings, get_settings
from .db import make_engine, make_sessionmaker
from .errors import install_error_handlers
from .geolocation.geofeed import FILE_NAME as GEOFEED_FILE
from .geolocation.geofeed import Geofeeds
from .geolocation.geoip import GeoIp
from .ipdata import admin as ipdata_admin
from .ipdata import router as ipdata_router
from .ipdata import runtime as ipdata_runtime
from .ipdata.downloader import DataUpdater
from .ipdata.service import IpDataService
from .logging_config import configure_logging
from .measurements import router as measurements_router
from .measurements import text as text_router
from .measurements.engine import MeasurementEngine
from .models import AppSetting, Base, User
from .nodes import router as nodes_router
from .nodes.autofill import refresh_all
from .nodes.registry import NodeRegistry
from .public_config import router as config_router
from .security.targets import Resolver, system_resolver
from .watches import router as watches_router
from .watches.service import WatchScheduler

log = logging.getLogger(__name__)


async def _setup_token(sessionmaker: Any, settings: Settings) -> str | None:
    """The one-time token for creating the first administrator, shared by every process.

    From `IPPING_SETUP_TOKEN` when set. Otherwise one is generated and stored, so that all backend
    processes accept the same token; the process that creates it is the one that logs it.
    """
    async with sessionmaker() as db:
        if (await db.scalar(select(func.count()).select_from(User))) != 0:
            return None
        if settings.setup_token:
            return settings.setup_token
        stored = await db.get(AppSetting, SETUP_TOKEN_KEY)
        if stored is not None:
            return str(stored.value)
        token = secrets.token_urlsafe(24)
        db.add(AppSetting(key=SETUP_TOKEN_KEY, value=token))
        try:
            await db.commit()
        except IntegrityError:  # another process generated one first: use theirs
            await db.rollback()
            stored = await db.get(AppSetting, SETUP_TOKEN_KEY)
            return str(stored.value) if stored is not None else token
    log.warning(
        "No administrator exists yet. Open /setup and enter this one-time setup token: %s", token
    )
    return token


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
        cluster = Cluster(
            sessionmaker,
            db_engine,
            settings.database_url,
            heartbeat_s=settings.instance_heartbeat_s,
            dead_after_s=settings.instance_dead_after_s,
        )
        engine = MeasurementEngine(
            sessionmaker,
            registry,
            cluster,
            resolver,
            dispatch_poll_s=settings.dispatch_poll_s,
            reaper_interval_s=settings.reaper_interval_s,
            pending_grace_s=settings.pending_grace_s,
        )
        cluster.on_command(engine.handle_command)
        cluster.on_live_connections(
            lambda: [
                (c.node_id, c.capabilities, c.protocol_version) for c in registry.connections()
            ]
        )
        app.state.settings = settings
        app.state.db_engine = db_engine
        app.state.sessionmaker = sessionmaker
        app.state.registry = registry
        app.state.cluster = cluster
        app.state.engine = engine
        scheduler = WatchScheduler(sessionmaker, engine, resolver=resolver)
        app.state.watches = scheduler
        app.state.geoip = GeoIp.from_settings(
            settings.geoip_dir, settings.geoip_city_db, settings.geoip_asn_db
        )
        app.state.geofeeds = Geofeeds(
            Path(settings.geoip_dir) / GEOFEED_FILE if settings.geoip_dir else None
        )

        # Every IP data source, kept up to date on disk and asked and voted on (see ipdata/)
        data_dir = Path(settings.geoip_dir) if settings.geoip_dir else None
        app.state.ipdata = IpDataService(
            lambda: app.state.geoip, data_dir, offline=settings.ipdata_offline
        )

        async def refresh_nodes() -> None:
            await refresh_all(sessionmaker, app.state.ipdata)

        app.state.dataupdater = DataUpdater(
            sessionmaker,
            settings,
            data_dir,
            lambda: app.state.geoip,
            offline=settings.ipdata_offline,
            on_updated=refresh_nodes,
        )

        if create_schema:
            async with db_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        else:
            from .migrate import ensure_schema_is_current

            await ensure_schema_is_current(db_engine)

        app.state.setup_token = await _setup_token(sessionmaker, settings)

        # Registering first matters: the reaper judges other instances by who is registered.
        await cluster.start()
        await engine.reap()  # settle anything an earlier process left behind, before serving
        loops = [
            asyncio.create_task(engine.run_dispatcher(), name="dispatcher"),
            asyncio.create_task(engine.run_reaper(), name="reaper"),
        ]
        loops.append(asyncio.create_task(maintenance.loop(sessionmaker), name="maintenance"))
        loops.append(asyncio.create_task(scheduler.run(), name="watches"))
        loops.append(asyncio.create_task(app.state.dataupdater.run(), name="ipdata-update"))
        loops.append(
            asyncio.create_task(ipdata_runtime.settings_loop(app.state), name="ipdata-settings")
        )
        app.state.loops = loops  # tests stop these to simulate a process that is simply gone
        try:
            yield
        finally:
            for loop in loops:
                loop.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            await engine.shutdown()
            await cluster.stop()
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
    app.include_router(watches_router.router)
    app.include_router(api_keys_router.router)
    app.include_router(nodes_router.router)
    app.include_router(measurements_router.router)
    app.include_router(ipdata_admin.router)
    app.include_router(ipdata_router.router)
    app.include_router(text_router.router)
    app.include_router(config_router)

    @app.get("/api/v1/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/v1/health/ready", tags=["health"])
    async def ready() -> dict[str, str]:
        async with app.state.sessionmaker() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ready"}

    return app
