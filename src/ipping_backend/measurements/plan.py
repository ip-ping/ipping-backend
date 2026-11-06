"""What each kind of measurement asks of a node, and what it costs.

Pure functions, so the rules for a kind live in one place: its options, how many samples a task
can produce, how it counts against the probe budget, and how long it may take.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..runtime_settings import RuntimeSettings
from ..security.targets import ParsedTarget
from .schemas import CreateMeasurementRequest

INTERVAL_MS = 1000
PING_TIMEOUT_MS = 2000
TCP_TIMEOUT_MS = 3000
HTTP_TIMEOUT_MS = 10_000  # for the whole request, redirects included
HOP_TIMEOUT_MS = 1000

# An HTTP check is a handful of packets and a TLS handshake: more than a ping, far less than a
# traceroute. The unit is "one probe a node sends".
HTTP_COST = 2


class PlanError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Plan:
    options: dict[str, Any]
    port: int | None
    deadline_s: int


def sample_limit(kind: str, options: dict[str, Any]) -> int:
    """The most samples one task of this kind can produce."""
    if kind == "http":
        return 1
    if kind == "traceroute":
        return int(options["max_hops"])
    return int(options["count"])


def probe_cost(kind: str, options: dict[str, Any]) -> int:
    """What one task of this kind counts for against the probe budget. A traceroute sends three
    packets per hop but counts once per hop."""
    if kind == "http":
        return HTTP_COST
    if kind == "traceroute":
        return int(options["max_hops"])
    return int(options["count"])


def _port(
    request: CreateMeasurementRequest, parsed: ParsedTarget, settings: RuntimeSettings
) -> int:
    port = request.port or parsed.port
    if port is None:
        raise PlanError("port_required", "Enter a port, for example example.com:443.")
    if port not in settings.allowed_ports:
        allowed = ", ".join(str(p) for p in settings.allowed_ports)
        raise PlanError(
            "port_not_allowed", f"Port {port} is not allowed on this service. Allowed: {allowed}."
        )
    return port


def plan_measurement(
    request: CreateMeasurementRequest, parsed: ParsedTarget, settings: RuntimeSettings
) -> Plan:
    kind = request.kind
    if kind not in settings.enabled_kinds:
        raise PlanError("kind_disabled", f"{kind} measurements are turned off on this service.")
    if kind in ("http", "traceroute") and request.count is not None:
        raise PlanError("invalid_option", f"A {kind} measurement does not take a count.")
    if kind != "http" and request.follow_redirects:
        raise PlanError("invalid_option", "Only an http measurement can follow redirects.")
    if kind not in ("tcp", "http") and request.port is not None:
        raise PlanError("invalid_option", f"A {kind} measurement does not take a port.")

    deadline = settings.measurement_deadline_s
    if kind in ("ping", "tcp"):
        count = request.count or settings.default_count
        if count > settings.max_count:
            raise PlanError("count_too_high", f"At most {settings.max_count} probes per node.")
        timeout = PING_TIMEOUT_MS if kind == "ping" else TCP_TIMEOUT_MS
        options: dict[str, Any] = {
            "count": count,
            "interval_ms": INTERVAL_MS,
            "timeout_ms": timeout,
        }
        port = None
        if kind == "tcp":
            port = _port(request, parsed, settings)
            options["port"] = port
        slowest = count * INTERVAL_MS // 1000 + timeout // 1000
        return Plan(options, port, max(deadline, slowest + 10))

    if kind == "http":
        scheme = parsed.scheme or ("http" if parsed.port in (80, 8080) else "https")
        port = request.port or parsed.port or (80 if scheme == "http" else 443)
        if port not in settings.allowed_ports:
            allowed = ", ".join(str(p) for p in settings.allowed_ports)
            raise PlanError(
                "port_not_allowed",
                f"Port {port} is not allowed on this service. Allowed: {allowed}.",
            )
        redirects = min(5, settings.max_redirects) if request.follow_redirects else 0
        options = {
            "count": 1,
            "interval_ms": INTERVAL_MS,
            "timeout_ms": HTTP_TIMEOUT_MS,
            "port": port,
            "scheme": scheme,
            "path": parsed.path or "/",
            "max_redirects": redirects,
        }
        return Plan(options, port, max(deadline, HTTP_TIMEOUT_MS // 1000 + 10))

    hops = settings.traceroute_max_hops
    options = {
        "count": 1,
        "interval_ms": INTERVAL_MS,
        "timeout_ms": HOP_TIMEOUT_MS,
        "max_hops": hops,
    }
    return Plan(options, None, max(deadline, hops * HOP_TIMEOUT_MS // 1000 + 15))
