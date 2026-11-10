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
TLS_TIMEOUT_MS = 8000  # connect and handshake together
HOP_TIMEOUT_MS = 1000
DNS_TIMEOUT_MS = 5000

# The public resolvers a DNS check may ask, by name. The node asks the address on port 53; it refuses
# anything that is not a public address, so this list is the whole set of outside servers a
# measurement can send a query to.
DNS_RESOLVERS: dict[str, str] = {
    "cloudflare": "1.1.1.1",
    "google": "8.8.8.8",
    "quad9": "9.9.9.9",
}

# The same resolvers over HTTPS (RFC 8484): the name their certificate is for, and the path of the endpoint.
# The node asks the resolver's address on port 443 and checks the certificate for that name. Quad9 answers only
# over HTTP/2 (HTTP 505 for HTTP/1.1), which nodes speak from v0.7.0 (see `protocol.HTTP2_ONLY_DOH`).
DOH_RESOLVERS: dict[str, tuple[str, str]] = {
    "cloudflare": ("cloudflare-dns.com", "/dns-query"),
    "google": ("dns.google", "/dns-query"),
    "quad9": ("dns.quad9.net", "/dns-query"),
}

# An HTTP check is a handful of packets and a TLS handshake: more than a ping, far less than a
# traceroute. The unit is "one probe a node sends".
HTTP_COST = 2
TLS_COST = 2  # a connection and a handshake


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


def plan_lookup(record_type: str) -> Plan:
    """One question to a node's own resolvers, asked on the backend's behalf to learn an address.

    It is not a measurement a person asked for, so it does not depend on which kinds the administrator
    turned on (a person may not ask for a dns check and still choose a node to resolve a name).
    """
    options = {
        "count": 1,
        "interval_ms": INTERVAL_MS,
        "timeout_ms": DNS_TIMEOUT_MS,
        "record_type": record_type,
        "resolver": "system",
    }
    return Plan(options, None, DNS_TIMEOUT_MS // 1000 + 5)


def sample_limit(kind: str, options: dict[str, Any]) -> int:
    """The most samples one task of this kind can produce."""
    if kind in ("http", "dns", "tls"):
        return 1
    if kind == "traceroute":
        return int(options["max_hops"])
    return int(options["count"])


def probe_cost(kind: str, options: dict[str, Any]) -> int:
    """What one task of this kind counts for against the probe budget. A traceroute sends three
    packets per hop but counts once per hop; every extra round adds one packet per hop, a third of
    a count."""
    if kind == "http":
        return HTTP_COST
    if kind == "tls":
        return TLS_COST
    if kind == "dns":
        return 1
    if kind == "traceroute":
        hops, rounds = int(options["max_hops"]), int(options["count"])
        return -(-hops * (rounds + 2) // 3)
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
    if kind in ("http", "dns", "tls") and request.count is not None:
        raise PlanError("invalid_option", f"A {kind} measurement does not take a count.")
    if kind != "http" and request.follow_redirects:
        raise PlanError("invalid_option", "Only an http measurement can follow redirects.")
    if kind not in ("tcp", "http", "tls") and request.port is not None:
        raise PlanError("invalid_option", f"A {kind} measurement does not take a port.")
    if kind != "dns" and (request.record_type is not None or request.resolver is not None):
        raise PlanError(
            "invalid_option", "Only a dns measurement takes a record type or a resolver."
        )
    if request.transport is not None:
        # udp and doh are the ways to ask DNS; tcp and quic are the ways to reach a TLS server.
        transports = {"dns": ("udp", "doh"), "tls": ("tcp", "quic")}.get(kind)
        if transports is None:
            raise PlanError("invalid_option", "Only a dns or tls measurement takes a transport.")
        if request.transport not in transports:
            raise PlanError(
                "invalid_option",
                f"A {kind} measurement's transport is {' or '.join(transports)}, "
                f"not {request.transport}.",
            )

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

    if kind == "tls":
        # A name is presented to the server (SNI) and checked against the certificate; an address is
        # checked as an address. The port defaults to 443 and must be one the service allows.
        port = request.port or parsed.port or 443
        if port not in settings.allowed_ports:
            allowed = ", ".join(str(p) for p in settings.allowed_ports)
            raise PlanError(
                "port_not_allowed",
                f"Port {port} is not allowed on this service. Allowed: {allowed}.",
            )
        options = {
            "count": 1,
            "interval_ms": INTERVAL_MS,
            "timeout_ms": TLS_TIMEOUT_MS,
            "port": port,
        }
        if request.transport == "quic":
            options["transport"] = "quic"
        return Plan(options, port, max(deadline, TLS_TIMEOUT_MS // 1000 + 10))

    if kind == "dns":
        if parsed.ip is not None:
            raise PlanError("invalid_target", "A DNS check needs a name, not an IP address.")
        resolver = request.resolver or "system"
        options = {
            "count": 1,
            "interval_ms": INTERVAL_MS,
            "timeout_ms": DNS_TIMEOUT_MS,
            "record_type": request.record_type or "A",
            "resolver": resolver,
        }
        if resolver != "system":
            options["resolver_ip"] = DNS_RESOLVERS[resolver]
        if request.transport == "doh":
            if resolver == "system":
                raise PlanError(
                    "invalid_option",
                    "DNS over HTTPS asks a public resolver (cloudflare, google or quad9), not each "
                    "node's own.",
                )
            host, path = DOH_RESOLVERS[resolver]
            options.update(transport="doh", doh_host=host, doh_path=path)
        return Plan(options, None, max(deadline, DNS_TIMEOUT_MS // 1000 + 10))

    hops = settings.traceroute_max_hops
    # `count` is the number of rounds: 1 traces once, more repeats the trace like mtr to show loss.
    rounds = request.count or 1
    if rounds > settings.max_count:
        raise PlanError("count_too_high", f"At most {settings.max_count} rounds per node.")
    options = {
        "count": rounds,
        "interval_ms": INTERVAL_MS,
        "timeout_ms": HOP_TIMEOUT_MS,
        "max_hops": hops,
    }
    slowest = hops * HOP_TIMEOUT_MS // 1000 + (rounds - 1) * (INTERVAL_MS + HOP_TIMEOUT_MS) // 1000
    return Plan(options, None, max(deadline, slowest + 15))
