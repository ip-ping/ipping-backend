"""Turn a finished measurement into a stored location estimate."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import LocationEstimate, Measurement, MeasurementTask, utcnow
from . import estimate as E
from .estimate import haversine_km
from .geoip import GeoIp, rule_out, valid_ip
from .schemas import (
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


def _to_out(
    measurement: Measurement, estimate: E.Estimate, observations: list[E.Observation]
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
        target_ip=measurement.target_ip,
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
        warnings=list(estimate.warnings),
        limitations=list(estimate.limitations),
    )


def with_geoip(out: LocationEstimateOut, geoip: GeoIp | None) -> LocationEstimateOut:
    """Add what the configured databases say, checked against this result's own measurements.

    Done on every request rather than stored with the estimate: the databases are replaced every
    month and the estimate is not.
    """
    ip = valid_ip(out.target_ip)
    if geoip is None or ip is None:
        return out
    out = out.model_copy(deep=True)
    asn = geoip.asn(ip)
    if asn is not None:
        out.asn = AsnOut(number=asn.number, organization=asn.organization)
    place = geoip.place(ip)
    if place is None:
        return out
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
    if not verified:
        out.geoip = GeoIpOut(
            source=geoip.source,
            place=shown,
            verdict="not_checked",
            distance_to_estimate_km=to_estimate,
            attribution=geoip.attribution,
        )
        return out
    ruled = rule_out(place, verified)
    out.geoip = GeoIpOut(
        source=geoip.source,
        place=shown,
        verdict="ruled_out" if ruled else "consistent",
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
        attribution=geoip.attribution,
    )
    return out


async def location_for(
    db: AsyncSession, measurement: Measurement, geoip: GeoIp | None = None
) -> LocationEstimateOut:
    return with_geoip(await _stored_or_new(db, measurement), geoip)


async def _stored_or_new(db: AsyncSession, measurement: Measurement) -> LocationEstimateOut:
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
    if measurement.dns_mode != "fixed" or not measurement.target_ip:
        return _unavailable(
            measurement,
            "unavailable",
            "An estimate needs every node to measure the same address. Run the measurement with "
            "“Same IP for every node”.",
        )

    stored = await db.get(LocationEstimate, measurement.id)
    if stored is not None and stored.model_version == E.MODEL_VERSION:
        return LocationEstimateOut.model_validate(stored.result)

    tasks = list(
        (
            await db.execute(
                select(MeasurementTask).where(MeasurementTask.measurement_id == measurement.id)
            )
        ).scalars()
    )
    observations = _observations(tasks)
    estimate = await asyncio.to_thread(E.estimate, observations)
    estimate.nodes_total = len(tasks)
    out = _to_out(measurement, estimate, observations)

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
