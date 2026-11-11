"""The IP data service: ask every source that is on, vote, and say how sure each answer is.

For an address, each enabled source is asked (the databases from disk, the online services over HTTPS, all at once), what comes
back is turned into one `Record` each, and `consensus.decide` takes a majority vote for every field. A source that is
off, has no key where it needs one, has no data yet, is rate limited or fails is listed with the reason and does not vote;
the others still answer. Nothing is cached for the databases (they are read from disk); an online answer is kept for a day and
a failure for two minutes, so a page that shows the same address again costs no second request.

`offline` (`IPPING_IPDATA_OFFLINE`) stops every outgoing request of this module: no lookups, no downloads.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .. import __version__
from ..geolocation.geoip import GeoIp
from ..security.targets import address_is_public
from .catalog import BY_ID, CATALOG, DATABASES, ONLINE, SourceInfo
from .consensus import decide
from .countries import CONTINENTS, NAMES
from .local import LocalSources
from .online import SOURCES as ONLINE_SOURCES
from .options import IpDataOptions
from .records import Record
from .schemas import IpInfoOut, SourceAnswerOut, SourceSaid

log = logging.getLogger(__name__)

SUCCESS_TTL_S = 24 * 3600.0
FAILURE_TTL_S = 120.0
CACHE_LIMIT = 4096
DEFAULT_PAUSE_S = 600.0
MAX_PAUSE_S = 3600.0
ASK_TIMEOUT_S = 5.0
IN_FLIGHT = 8


@dataclass
class Health:
    """What the last request to an online source came to, for the admin page."""

    at: float | None = None
    status: str | None = None
    message: str | None = None
    paused_until: float = 0.0


@dataclass
class Answer:
    status: str  # ok, empty, error, disabled, needs_setup, rate_limited, offline
    record: Record | None = None
    message: str | None = None


class IpDataService:
    def __init__(
        self,
        geoip_of: Callable[[], GeoIp],
        directory: Path | None,
        *,
        offline: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.local = LocalSources(geoip_of, directory)
        self._geoip_of = geoip_of
        self.offline = offline
        self._transport = transport
        self.options = IpDataOptions()
        self.keys: dict[str, dict[str, str]] = {}
        self.maxmind_ready = False  # whether the MaxMind account ID and license key are both set
        self._cache: dict[tuple[str, str], tuple[float, Answer]] = {}
        self.health: dict[str, Health] = {s: Health() for s in ONLINE}
        self._client: httpx.AsyncClient | None = None
        self._slots = asyncio.Semaphore(IN_FLIGHT)
        geoip_of().enabled = (
            self.enabled
        )  # the databases the rest of the backend uses honour the switches too

    # ---- settings
    def apply(
        self, options: IpDataOptions, keys: dict[str, dict[str, str]], *, maxmind_ready: bool
    ) -> None:
        """Use these settings from now on (they are read from the database by the caller)."""
        changed = options.source_enabled != self.options.source_enabled
        self.options = options
        self.keys = keys
        self.maxmind_ready = maxmind_ready
        geoip = self._geoip_of()
        geoip.enabled = self.enabled
        if changed:
            geoip.rescan()

    def enabled(self, source: str) -> bool:
        return self.options.enabled(source)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- the state of every source, for the admin page and for the answer
    def state(self, source: str) -> tuple[str, str | None]:
        """(`ready`, `disabled`, `needs_setup`, `waiting`, `offline`, `no_directory`, `paused`) and a sentence about it."""
        info = BY_ID[source]
        if not self.enabled(source):
            return "disabled", "Turned off."
        if info.kind == "online":
            if self.offline:
                return (
                    "offline",
                    "Outgoing lookups are turned off on this server (IPPING_IPDATA_OFFLINE).",
                )
            health = self.health[source]
            if health.paused_until > time.monotonic():
                minutes = max(1, round((health.paused_until - time.monotonic()) / 60))
                return (
                    "paused",
                    f"Rate limited: not asked for another {minutes} min. A key raises the limit.",
                )
            return "ready", None
        if source == "maxmind" and not self.maxmind_ready and not self.local.present(source):
            return "needs_setup", "Needs a MaxMind account ID and license key."
        if self.local.directory is None and not self.local.present(source):
            return (
                "no_directory",
                "The backend has no directory to keep databases in (IPPING_GEOIP_DIR).",
            )
        if not self.local.present(source):
            return (
                "waiting",
                "Not downloaded yet."
                if not self.offline
                else "Offline mode: nothing is downloaded.",
            )
        return "ready", None

    def available(self) -> bool:
        """Whether any source can answer now: a database with data, or an online source that is on (and not paused)."""
        return any(self.state(info.id)[0] == "ready" for info in CATALOG)

    # ---- asking
    async def lookup(self, address: str, *, online: bool = True) -> IpInfoOut:
        """Everything the enabled sources say about an address, voted. Raises ValueError for text that is not an address."""
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        text = str(ip)
        out = IpInfoOut(address=text, version=ip.version, public=address_is_public(ip), found=False)
        if not out.public:
            return out

        answers: dict[str, Answer] = {}
        for info in CATALOG:
            if info.kind == "database":
                answers[info.id] = self._ask_local(info, text)
        if online:
            asked = [s for s in ONLINE if self.enabled(s)]
            results = await asyncio.gather(*(self._ask_online(s, text) for s in asked))
            answers.update(dict(zip(asked, results, strict=True)))
        for info in CATALOG:
            if info.id not in answers:
                answers[info.id] = (
                    Answer("disabled", message="Turned off.")
                    if not self.enabled(info.id)
                    else Answer("offline", message="Not asked.")
                )
        return self._assemble(out, answers)

    def _ask_local(self, info: SourceInfo, address: str) -> Answer:
        status, message = self.state(info.id)
        if status != "ready":
            return Answer({"no_directory": "needs_setup"}.get(status, status), message=message)
        try:
            record = self.local.lookup(info.id, address)
        except Exception:
            log.exception("reading %s failed", info.id)
            return Answer("error", message="Could not read its data.")
        return (
            Answer("ok", record) if record is not None and not record.empty() else Answer("empty")
        )

    async def _ask_online(self, source: str, address: str) -> Answer:
        status, message = self.state(source)
        if status == "paused":
            return Answer("rate_limited", message=message)
        if status != "ready":
            return Answer(status, message=message)
        key = (source, address)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            async with self._slots, asyncio.timeout(ASK_TIMEOUT_S + 1):
                answer = await self._request(source, address)
        except TimeoutError:
            answer = Answer("error", message="Did not answer in time.")
        ttl = SUCCESS_TTL_S if answer.status in ("ok", "empty") else FAILURE_TTL_S
        if answer.status != "rate_limited":
            if len(self._cache) >= CACHE_LIMIT:
                for stale in list(self._cache)[: CACHE_LIMIT // 8]:
                    del self._cache[stale]
            self._cache[key] = (time.monotonic() + ttl, answer)
        health = self.health[source]
        health.at, health.status, health.message = time.time(), answer.status, answer.message
        return answer

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(ASK_TIMEOUT_S, connect=4.0),
                headers={"User-Agent": f"ipping-backend/{__version__}"},
                transport=self._transport,
                follow_redirects=False,
            )
        return self._client

    async def _request(self, source: str, address: str) -> Answer:
        spec = ONLINE_SOURCES[source]
        credentials = self.keys.get(source, {})
        url, headers, params = spec.request(address, credentials)
        try:
            response = await self._http().get(url, headers=headers, params=params)
        except httpx.TimeoutException:
            return Answer("error", message="Did not answer in time.")
        except httpx.HTTPError:  # the message of these can hold the URL, which holds the key
            return Answer("error", message="Could not be reached.")
        if response.status_code == 429:
            pause = DEFAULT_PAUSE_S
            try:
                pause = min(MAX_PAUSE_S, max(30.0, float(response.headers.get("Retry-After", ""))))
            except ValueError:
                pass
            self.health[source].paused_until = time.monotonic() + pause
            return Answer(
                "rate_limited", message="Its limit is used up for now. A key raises the limit."
            )
        if response.status_code in spec.refused:
            return Answer(
                "error",
                message="Refused the key."
                if credentials
                else "Refused the lookup: it needs a key.",
            )
        if response.status_code >= 400:
            return Answer("error", message=f"Answered HTTP {response.status_code}.")
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return Answer("error", message="Gave an answer that could not be read.")
        try:
            record = spec.parse(data)
        except Exception:
            log.exception("reading the answer of %s failed", source)
            return Answer("error", message="Gave an answer that could not be read.")
        return Answer("ok", record) if not record.empty() else Answer("empty")

    # ---- the answer
    def _assemble(self, out: IpInfoOut, answers: dict[str, Answer]) -> IpInfoOut:
        voted = decide({s: a.record for s, a in answers.items() if a.record is not None})
        record = voted.record
        if not voted.contributors:
            out.sources = [self._answer_out(info, answers[info.id], voted) for info in CATALOG]
            return out
        out.found = True
        out.network = record.network
        out.country = record.country
        out.country_name = NAMES.get(record.country) if record.country else None
        out.continent = CONTINENTS.get(record.country) if record.country else None
        out.region = record.region
        out.region_code = record.region_code
        out.city = record.city
        out.postal_code = record.postal_code
        out.latitude = record.latitude
        out.longitude = record.longitude
        out.accuracy_radius_km = record.accuracy_radius_km
        out.time_zone = record.time_zone
        out.asn = record.asn
        out.organization = record.organization
        out.confidence = dict(voted.percent)
        out.sources = [self._answer_out(info, answers[info.id], voted) for info in CATALOG]
        contributors = [BY_ID[s] for s in voted.contributors]
        out.source = ", ".join(i.name for i in contributors)
        notices = [i.attribution for i in contributors if i.attribution]
        out.attribution = "; ".join(notices) or None
        built: list[datetime] = [
            f["built"]
            for i in contributors
            if i.kind == "database"
            for f in self.local.files(i.id)
            if f.get("built")
        ]
        out.database_built = max(built) if built else None
        return out

    @staticmethod
    def _answer_out(info: SourceInfo, answer: Answer, voted: Any) -> SourceAnswerOut:
        said = answer.record.fields() if answer.record is not None else {}
        agrees = sorted(f for f, ids in voted.agree.items() if info.id in ids)
        differs = sorted(f for f, ids in voted.differ.items() if info.id in ids)
        return SourceAnswerOut(
            id=info.id,
            name=info.name,
            kind=info.kind,
            status=answer.status,
            message=answer.message,
            said=SourceSaid.model_validate(
                {k: v for k, v in said.items() if k in SourceSaid.model_fields}
            ),
            agrees=agrees,
            differs=differs,
        )


__all__ = ["DATABASES", "Answer", "IpDataService"]
