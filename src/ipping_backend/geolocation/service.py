"""Turn a finished measurement into a stored location estimate."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..ipdata.service import IpDataService, place_of
from ..models import LocationEstimate, Measurement, MeasurementTask, utcnow
from . import estimate as E
from .estimate import haversine_km
from .geofeed import Entry, Geofeeds, host_of
from .geoip import GeoPlace, rule_out, valid_ip
from .schemas import (
    AccuracyOut,
    AddressGroupOut,
    AlternativeOut,
    AsnOut,
    ClosestOut,
    EvidenceOut,
    GeoIpOut,
    LocationEstimateOut,
    PlaceOut,
    RuledOutOut,
)


def _unavailable(
    measurement: Measurement, status: Literal["unavailable", "pending"], summary: str
) -> LocationEstimateOut:
    return LocationEstimateOut(
        status=status,
        summary=summary,
        model_version=E.MODEL_VERSION,
        target_ip=measurement.target_ip,
        limitations=list(E.LIMITATIONS),
    )


def _observations(tasks: list[MeasurementTask]) -> list[E.Observation]:
    """One observation per node that heard from the target: its smallest valid round trip.

    A node that never got an answer says nothing about distance, so it is not evidence. Neither is
    one with no known coordinates.
    """
    result = []
    for task in tasks:
        info: dict[str, Any] = task.node_info or {}
        summary: dict[str, Any] = task.summary or {}
        lat, lon, rtt = info.get("latitude"), info.get("longitude"), summary.get("min_ms")
        if lat is None or lon is None or not summary.get("received") or rtt is None:
            continue
        if info.get("tier") == "community" and not info.get("location_verified"):
            # A volunteer's node says where it is and nobody has checked: it must not move an estimate.
            continue
        result.append(
            E.Observation(
                node_id=task.node_id,
                name=str(info.get("name", task.node_id)),
                latitude=float(lat),
                longitude=float(lon),
                rtt_ms=float(rtt),
                verified=bool(info.get("location_verified", False)),
            )
        )
    return result


class UnknownAddress(LookupError):
    """The address asked for was not measured by any node."""


def _address_of(task: MeasurementTask, measurement: Measurement) -> str | None:
    """The address this node measured. A fixed measurement gave every node the same one."""
    if task.tested_ip:
        return task.tested_ip
    return measurement.target_ip if measurement.dns_mode == "fixed" else None


def _groups(
    tasks: list[MeasurementTask], measurement: Measurement
) -> dict[str, list[MeasurementTask]]:
    """The tasks by the address they measured, the address most nodes measured first.

    When each node resolves the name itself they do not always get one address (geo-DNS, a CDN), and the round
    trips to different servers cannot be put into one estimate; the nodes that agree are.
    """
    by_ip: dict[str, list[MeasurementTask]] = {}
    for task in tasks:
        ip = _address_of(task, measurement)
        if ip is not None:
            by_ip.setdefault(ip, []).append(task)
    return dict(sorted(by_ip.items(), key=lambda item: (-len(item[1]), item[0])))


def _to_out(
    measurement: Measurement,
    estimate: E.Estimate,
    observations: list[E.Observation],
    *,
    target_ip: str | None,
    groups: dict[str, list[MeasurementTask]],
) -> LocationEstimateOut:
    used = {o.node_id for o in estimate.evidence}

    def place(p: E.Place) -> PlaceOut:
        return PlaceOut(name=p.name, country=p.country, latitude=p.latitude, longitude=p.longitude)

    closest = None
    if estimate.closest is not None:
        c = estimate.closest
        closest = ClosestOut(
            node_id=c.observation.node_id,
            node_name=c.observation.name,
            rtt_ms=round(c.observation.rtt_ms, 3),
            max_distance_km=round(c.max_distance_km),
            typical_distance_km=round(c.typical_distance_km),
        )
    return LocationEstimateOut(
        status=estimate.status,
        summary=estimate.summary,
        model_version=E.MODEL_VERSION,
        target_ip=target_ip,
        likely=place(estimate.likely) if estimate.likely else None,
        strength=estimate.strength,
        alternatives=[
            AlternativeOut(place=place(a.place), distance_km=int(a.distance_km))
            for a in estimate.alternatives
        ],
        closest=closest,
        evidence=[
            EvidenceOut(
                node_id=o.node_id,
                node_name=o.name,
                latitude=o.latitude,
                longitude=o.longitude,
                rtt_ms=round(o.rtt_ms, 3),
                verified=o.verified,
                used=o.node_id in used,
            )
            for o in sorted(observations, key=lambda o: o.rtt_ms)
        ],
        nodes_total=estimate.nodes_total,
        conflicting=estimate.conflicting,
        accuracy=_accuracy_out(estimate),
        warnings=_address_warning(target_ip, groups) + list(estimate.warnings),
        limitations=list(estimate.limitations),
        groups=_group_outs(target_ip, groups),
    )


def _accuracy_out(estimate: E.Estimate) -> AccuracyOut | None:
    if estimate.accuracy is None:
        return None
    return AccuracyOut(
        percent=estimate.accuracy.headline_pct,
        radius_km=estimate.accuracy.headline_km,
        by_radius={str(km): pct for km, pct in estimate.accuracy.by_radius.items()},
        basis=(
            f"Of answers made when the closest node was about this far away, on real targets of known position, "
            f"this share was within {estimate.accuracy.headline_km:,} km. It is a rate for answers like this one, "
            "not a probability for this address."
            + (
                f" The nodes disagree here by up to {round(estimate.disagreement_km):,} km, so the address "
                "may be served from several places: the figure is the one for a closest node that far away, "
                "a cautious stand-in (an address served from several places has no single truth to measure "
                "against), and the real figure is lower."
                if estimate.conflicting
                else ""
            )
        ),
    )


def _group_outs(
    selected: str | None, groups: dict[str, list[MeasurementTask]]
) -> list[AddressGroupOut]:
    if len(groups) < 2:
        return []
    return [
        AddressGroupOut(ip=ip, nodes=len(t), selected=ip == selected) for ip, t in groups.items()
    ]


def _address_warning(selected: str | None, groups: dict[str, list[MeasurementTask]]) -> list[str]:
    if len(groups) < 2 or selected not in groups:
        return []
    others = sum(len(t) for ip, t in groups.items() if ip != selected)
    return [
        f"The nodes resolved the name to {len(groups)} different addresses. This estimate is for "
        f"{selected}, measured by {len(groups[selected])} nodes; the other {others} measured "
        "something else and are left out."
    ]


def _checked(
    out: LocationEstimateOut,
    place: GeoPlace | None,
    source: str,
    *,
    attribution: str | None = None,
    claim: str | None = None,
    prefix: str | None = None,
) -> GeoIpOut:
    """A place somebody claims for the address, checked against this result's own verified nodes.

    `consistent` only means no measurement contradicts it; `ruled_out` names the node that does.
    """
    if place is None:  # a claim with no place the gazetteer knows: shown, not checked
        return GeoIpOut(
            source=source,
            claim=claim,
            prefix=prefix,
            verdict="not_checked",
            attribution=attribution,
        )
    verified = [
        (e.node_id, e.node_name, e.latitude, e.longitude, e.rtt_ms)
        for e in out.evidence
        if e.verified
    ]
    shown = PlaceOut(
        name=place.name, country=place.country, latitude=place.latitude, longitude=place.longitude
    )
    to_estimate = (
        round(
            haversine_km(place.latitude, place.longitude, out.likely.latitude, out.likely.longitude)
        )
        if out.likely
        else None
    )
    ruled = rule_out(place, verified) if verified else None
    return GeoIpOut(
        source=source,
        place=shown,
        claim=claim,
        prefix=prefix,
        verdict="not_checked" if not verified else "ruled_out" if ruled else "consistent",
        distance_to_estimate_km=to_estimate,
        ruled_out_by=RuledOutOut(
            node_id=ruled.node_id,
            node_name=ruled.node_name,
            rtt_ms=ruled.rtt_ms,
            distance_km=round(ruled.distance_km),
            max_distance_km=round(ruled.max_distance_km),
        )
        if ruled
        else None,
        attribution=attribution,
    )


def with_geoip(out: LocationEstimateOut, ipdata: IpDataService | None) -> LocationEstimateOut:
    """Add what the IP data sources say (their vote), checked against this result's own measurements.

    Done on every request rather than stored with the estimate: the databases are replaced regularly
    and the estimate is not.
    """
    ip = valid_ip(out.target_ip)
    if ipdata is None or ip is None:
        return out
    out = out.model_copy(deep=True)
    info = ipdata.lookup(ip)
    if info.asn is not None:
        out.asn = AsnOut(number=info.asn, organization=(info.organization or "")[:120])
    place = place_of(info)
    if place is None:
        return out
    out.geoip = _checked(out, place, info.source or "", attribution=info.attribution)
    return out


def _claim(entry: Entry) -> str:
    where = ", ".join(part for part in (entry.city, entry.region, entry.country) if part)
    return where or "no place"


def with_geofeed(out: LocationEstimateOut, geofeeds: Geofeeds | None) -> LocationEstimateOut:
    """Add what the address's network says about itself in a geofeed the administrator listed.

    Looked up on every request from the stored file, like the GeoIP databases, and checked the same way. It
    never changes the estimate.
    """
    ip = valid_ip(out.target_ip)
    found = geofeeds.lookup(ip) if geofeeds is not None and ip is not None else None
    if geofeeds is None or found is None:
        return out
    entry, prefix = found
    out = out.model_copy(deep=True)
    out.geofeed = _checked(
        out,
        geofeeds.place(entry),
        f"Geofeed from {host_of(entry.source)}",
        claim=_claim(entry),
        prefix=prefix,
    )
    return out


async def location_for(
    db: AsyncSession,
    measurement: Measurement,
    ipdata: IpDataService | None = None,
    geofeeds: Geofeeds | None = None,
    ip: str | None = None,
) -> LocationEstimateOut:
    """The estimate for the address most nodes measured, or for `ip` when asked (see `UnknownAddress`)."""
    return with_geofeed(with_geoip(await _stored_or_new(db, measurement, ip), ipdata), geofeeds)


async def _stored_or_new(
    db: AsyncSession, measurement: Measurement, ip: str | None = None
) -> LocationEstimateOut:
    if measurement.status == "running":
        return _unavailable(
            measurement, "pending", "The estimate is made once every node has finished."
        )
    if measurement.kind not in ("ping", "tcp"):
        return _unavailable(
            measurement,
            "unavailable",
            "An estimate needs round-trip times to the target. Run a ping or TCP measurement.",
        )

    stored = await db.get(LocationEstimate, measurement.id)
    if (
        stored is not None
        and stored.model_version == E.MODEL_VERSION
        and (ip is None or ip == stored.result.get("target_ip"))
    ):
        return LocationEstimateOut.model_validate(stored.result)

    tasks = list(
        (
            await db.execute(
                select(MeasurementTask).where(MeasurementTask.measurement_id == measurement.id)
            )
        ).scalars()
    )
    groups = _groups(tasks, measurement)
    if not groups:
        return _unavailable(
            measurement,
            "unavailable",
            "No node reported which address it measured, so there is nothing to estimate from.",
        )
    chosen = ip if ip is not None else next(iter(groups))
    if chosen not in groups:
        raise UnknownAddress(chosen)
    group_tasks = groups[chosen]
    observations = _observations(group_tasks)
    estimate = await asyncio.to_thread(E.estimate, observations)
    estimate.nodes_total = len(group_tasks)
    out = _to_out(measurement, estimate, observations, target_ip=chosen, groups=groups)
    if ip is not None and chosen != next(iter(groups)):
        return out  # another address than the usual one: worked out for this request only

    # Results are immutable once a measurement has finished, so the estimate is stored; it is redone
    # only when the model changes.
    payload = out.model_dump(mode="json")
    if stored is None:
        db.add(
            LocationEstimate(
                measurement_id=measurement.id, model_version=E.MODEL_VERSION, result=payload
            )
        )
    else:
        stored.model_version, stored.result, stored.computed_at = E.MODEL_VERSION, payload, utcnow()
    try:
        await db.commit()
    except IntegrityError:  # another request stored it first; ours is identical
        await db.rollback()
    return out
