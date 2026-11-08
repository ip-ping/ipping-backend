"""Whether a run of a watch was good, and when that is worth telling anyone.

Pure functions, so the rules are in one place and can be tested without a database or a clock.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from ..measurements.schemas import MeasurementOut, TaskOut

# A certificate that has this long left or less is worth a warning before it is a failure.
TLS_EXPIRY_WARNING_DAYS = 14

Status = Literal["ok", "degraded", "down", "error"]
Event = Literal["alert", "recovered"]


@dataclass(frozen=True)
class Assessment:
    status: Status
    nodes_total: int
    nodes_ok: int
    median_ms: float | None = None
    min_ms: float | None = None
    max_ms: float | None = None
    note: str | None = None


def _days_left(task: TaskOut, now: datetime) -> float | None:
    """Days until the certificate this node was shown expires, if it trusted it."""
    first = task.samples[0] if task.samples else None
    cert = first.tls.tls if first and first.tls else None
    if cert is None or not cert.verified or cert.not_after is None:
        return None
    return (cert.not_after - now).total_seconds() / 86400


def task_ok(kind: str, task: TaskOut, now: datetime | None = None) -> bool:
    """Did this node get a good answer? What "good" means depends on what was asked.

    A reply to a ping or a connection that opened; an HTTP answer that is not a server error (a refused
    certificate, which sends no request, is not an answer); a DNS answer with a NOERROR code and at
    least one record; a route that reached the destination; a certificate this node trusts that does
    not expire within `TLS_EXPIRY_WARNING_DAYS`.
    """
    first = task.samples[0] if task.samples else None
    if kind == "tls":
        left = _days_left(task, now or datetime.now(UTC))
        return left is not None and left >= TLS_EXPIRY_WARNING_DAYS
    if kind == "http":
        return bool(
            first
            and first.http
            and first.http.status_code is not None
            and first.http.status_code < 500
        )
    if kind == "dns":
        return bool(first and first.dns and first.dns.rcode == "NOERROR" and first.dns.answers)
    if kind == "traceroute":
        return bool(task.summary and task.summary.reached)
    return bool(task.summary and task.summary.received > 0)


def assess(measurement: MeasurementOut, min_ok_pct: int, now: datetime | None = None) -> Assessment:
    """Judge a finished measurement. `error` is the check itself failing (no node took part, the
    service restarted), which says nothing about the target and never raises an alert."""
    tasks = measurement.tasks
    if measurement.status in ("interrupted", "cancelled"):
        return Assessment("error", len(tasks), 0, note=f"the measurement was {measurement.status}")
    if not tasks:
        return Assessment("error", 0, 0, note="no node took part")
    now = now or datetime.now(UTC)
    good = [t for t in tasks if task_ok(measurement.kind, t, now)]
    medians = [t.summary.median_ms for t in good if t.summary and t.summary.median_ms is not None]
    ok = len(good)
    note: str | None = None
    if ok == 0:
        status: Status = "down"
        if measurement.kind == "tls":
            # A certificate that is still valid but nearly out of time is a warning, not an outage.
            left = [d for t in tasks if (d := _days_left(t, now)) is not None]
            if left:
                status = "degraded"
                note = f"the certificate expires in {max(0, int(min(left)))} days"
    elif ok * 100 < min_ok_pct * len(tasks):
        status = "degraded"
    else:
        status = "ok"
    return Assessment(
        status,
        len(tasks),
        ok,
        median_ms=round(statistics.median(medians), 3) if medians else None,
        min_ms=round(min(medians), 3) if medians else None,
        max_ms=round(max(medians), 3) if medians else None,
        note=note,
    )


@dataclass(frozen=True)
class Streak:
    bad_streak: int
    alerting: bool
    event: Event | None


def advance(bad_streak: int, alerting: bool, status: Status, alert_after: int) -> Streak:
    """Update the run of bad results. Alert once when it reaches `alert_after` (not on every bad run),
    and say so once when the target is good again."""
    if status == "error":
        return Streak(bad_streak, alerting, None)
    if status in ("degraded", "down"):
        streak = bad_streak + 1
        if streak >= alert_after and not alerting:
            return Streak(streak, True, "alert")
        return Streak(streak, alerting, None)
    return Streak(0, False, "recovered" if alerting else None)
