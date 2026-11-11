"""The IP data service: ask every source that is on, vote, and say how sure each answer is.

For an address, each enabled source is asked (all from disk: nothing is fetched from anywhere when an address is looked up),
what comes back is turned into one `Record` each, and `consensus.decide` takes a majority vote for every field. A source that is off,
has no key where it needs one to be downloaded, has no data yet or fails is listed with the reason and does not vote; the
others still answer. The same vote is what the lookup API, a measurement's addresses, the location cross-check, the labels
of a traced path and a node's own details are built on, so every source counts the same wherever its data is used.

`offline` (`IPPING_IPDATA_OFFLINE`) stops every outgoing request of this module: nothing is downloaded, and the databases already on
disk still answer.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..geolocation.geoip import GeoIp, GeoPlace
from ..security.targets import address_is_public
from .catalog import BY_ID, CATALOG, SourceInfo, fetchable
from .consensus import decide
from .countries import CONTINENTS, NAMES
from .local import LocalSources
from .options import IpDataOptions
from .records import Record
from .schemas import IpInfoOut, SourceAnswerOut, SourceSaid

log = logging.getLogger(__name__)


@dataclass
class Answer:
    status: str  # ok, empty, error, disabled, needs_setup, waiting, offline
    record: Record | None = None
    message: str | None = None


def _sentence(label: str) -> str:
    return label[0].lower() + label[1:]


def place_of(info: IpInfoOut) -> GeoPlace | None:
    """The place an answer puts its address at, for checking it against measurements: the position, named for its city (or its country)."""
    if info.latitude is None or info.longitude is None:
        return None
    return GeoPlace(
        name=str(info.city or info.country_name or "Unknown")[:80],
        country=(info.country or "")[:2],
        latitude=info.latitude,
        longitude=info.longitude,
    )


class IpDataService:
    def __init__(
        self,
        geoip_of: Callable[[], GeoIp],
        directory: Path | None,
        *,
        offline: bool = False,
    ) -> None:
        self.local = LocalSources(geoip_of, directory)
        self.offline = offline
        self.options = IpDataOptions()
        self.keys: dict[str, dict[str, str]] = {}

    # ---- settings
    def apply(self, options: IpDataOptions, keys: dict[str, dict[str, str]]) -> None:
        """Use these settings from now on (they are read from the database by the caller)."""
        self.options = options
        self.keys = keys

    def enabled(self, source: str) -> bool:
        return self.options.enabled(source)

    @property
    def directory(self) -> Path | None:
        return self.local.directory

    # ---- the state of every source, for the admin page and for the answer
    def state(self, source: str) -> tuple[str, str | None]:
        """(`ready`, `disabled`, `needs_setup`, `waiting`, `offline`, `no_directory`) and a sentence about it."""
        info = BY_ID[source]
        if not self.enabled(source):
            return "disabled", "Turned off."
        if self.local.present(source):
            return "ready", None
        if not fetchable(source, self.keys):
            needs = " and ".join(_sentence(c.label) for c in info.credentials if c.required)
            return "needs_setup", f"Needs its {needs} to be downloaded."
        if self.local.directory is None:
            return (
                "no_directory",
                "The backend has no directory to keep databases in (IPPING_GEOIP_DIR).",
            )
        if self.offline:
            return "offline", "Offline mode: nothing is downloaded."
        return "waiting", "Not downloaded yet."

    def in_use(self) -> list[SourceInfo]:
        """The sources that can answer now."""
        return [info for info in CATALOG if self.state(info.id)[0] == "ready"]

    def available(self) -> bool:
        """Whether any source can answer now."""
        return bool(self.in_use())

    def knowing(self, *kinds: str) -> list[SourceInfo]:
        """The sources that can answer now and have some of these kinds of data (`position`, `asn`, ...) in the files they have."""
        wanted = set(kinds)
        return [i for i in self.in_use() if wanted & self.local.knows(i.id)]

    def can(self, kind: str) -> bool:
        """Whether a source that can answer now has this kind of data."""
        return bool(self.knowing(kind))

    # ---- asking
    def lookup(self, address: str) -> IpInfoOut:
        """Everything the enabled sources say about an address, voted. Raises ValueError for text that is not an address."""
        ip = ipaddress.ip_address(address)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        text = str(ip)
        out = IpInfoOut(address=text, version=ip.version, public=address_is_public(ip), found=False)
        if not out.public:
            return out
        answers = {info.id: self._ask(info, text) for info in CATALOG}
        return self._assemble(out, answers)

    def _ask(self, info: SourceInfo, address: str) -> Answer:
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
            f["built"] for i in contributors for f in self.local.files(i.id) if f.get("built")
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
            status=answer.status,
            message=answer.message,
            said=SourceSaid.model_validate(
                {k: v for k, v in said.items() if k in SourceSaid.model_fields}
            ),
            agrees=agrees,
            differs=differs,
        )
