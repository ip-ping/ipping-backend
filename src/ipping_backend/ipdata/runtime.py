"""Keeping the IP data service's view of the settings current, and the lookups the rest of the backend asks of it."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .options import load_credentials, load_keys, load_options

log = logging.getLogger(__name__)

SETTINGS_EVERY_S = 15.0


async def reload(state: Any) -> None:
    """Read the options and keys from the database into the service (another process may have changed them)."""
    async with state.sessionmaker() as db:
        options = await load_options(db)
        keys = await load_keys(db)
        credentials = await load_credentials(db, state.settings)
    state.ipdata.apply(options, keys, maxmind_ready=credentials.usable)


async def settings_loop(state: Any) -> None:
    while True:
        try:
            await reload(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reading the IP data settings failed")
        await asyncio.sleep(SETTINGS_EVERY_S)
