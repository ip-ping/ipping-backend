"""What a user may ask the network to probe.

Two steps, kept separate on purpose:

1. `parse_target` turns whatever was typed (an IP, a hostname, a pasted URL) into a host. Only the
   host survives: credentials, ports, paths and query strings are dropped here, so nothing
   sensitive can reach a log line or the database.
2. `resolve_target` resolves hostnames and applies the address policy to every address returned,
   then pins the address that will actually be probed. A hostname is never validated and then
   resolved a second time by something else (the DNS-rebinding trap).

Nodes repeat both checks locally; this module is the backend half.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

MAX_INPUT_LENGTH = 2048

# Suffixes that only make sense inside a private network (or that are reserved for it).
_INTERNAL_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home",
    ".corp",
    ".home.arpa",
    ".onion",
)
_LABEL = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")

# Ranges that Python's `is_global` accepts but that embed or translate to another address, which
# could smuggle a private IPv4 destination past the check.
_EXTRA_BLOCKED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "64:ff9b::/96",  # NAT64
        "64:ff9b:1::/48",  # local-use NAT64
        "2002::/16",  # 6to4
        "2001::/32",  # Teredo
    )
)


class TargetError(Exception):
    """A target that is refused. `code` is stable and safe to show to API clients."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ParsedTarget:
    host: str  # hostname (ASCII/punycode) or a normalised IP literal
    ip: IPAddress | None
    input_kind: str  # "ip" | "hostname" | "url"


@dataclass(frozen=True)
class Denylist:
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    hostnames: tuple[str, ...] = ()

    @classmethod
    def from_entries(cls, entries: list[str]) -> Denylist:
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        hostnames: list[str] = []
        for raw in entries:
            entry = raw.strip().lower().rstrip(".")
            if not entry:
                continue
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                hostnames.append(entry)
        return cls(tuple(networks), tuple(hostnames))

    def blocks_host(self, host: str) -> bool:
        return any(host == h or host.endswith("." + h) for h in self.hostnames)

    def blocks_ip(self, ip: IPAddress) -> bool:
        return any(ip.version == net.version and ip in net for net in self.networks)


def normalise_ip(ip: IPAddress) -> IPAddress:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def address_is_public(ip: IPAddress) -> bool:
    ip = normalise_ip(ip)
    # `is_global` alone still accepts multicast, so the unusable categories are named explicitly.
    if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    return not any(ip.version == net.version and ip in net for net in _EXTRA_BLOCKED)


def _invalid(message: str) -> TargetError:
    return TargetError("invalid_target", message)


def _extract_host(text: str) -> tuple[str, bool]:
    """Return (host, came_from_url_or_host_with_extras)."""
    try:
        if "://" in text:
            parts = urlsplit(text)
            if parts.scheme.lower() not in ("http", "https"):
                raise TargetError(
                    "unsupported_scheme", "Only http:// and https:// URLs are accepted."
                )
            host, from_url = parts.hostname, True
            _ = parts.port  # raises ValueError for an out-of-range port
        elif any(c in text for c in "/?#@"):
            parts = urlsplit("//" + text)
            host, from_url = parts.hostname, True
            _ = parts.port
        elif text.startswith("["):
            parts = urlsplit("//" + text)
            host, from_url = parts.hostname, False
            _ = parts.port
        elif text.count(":") == 1:  # host:port or ipv4:port; a bare IPv6 has more colons
            host, port = text.rsplit(":", 1)
            if not port.isdigit() or not 0 < int(port) < 65536:
                raise _invalid("That does not look like a valid host and port.")
            from_url = False
        else:
            host, from_url = text, False
    except ValueError as exc:
        raise _invalid("That does not look like a valid address or URL.") from exc
    if not host:
        raise _invalid("No host found in the input.")
    return host, from_url


def parse_target(raw: str) -> ParsedTarget:
    text = raw.strip()
    if not text:
        raise _invalid("Enter an IP address, hostname or URL.")
    if len(text) > MAX_INPUT_LENGTH:
        raise _invalid("The input is too long.")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in text):
        raise _invalid("The input contains whitespace or control characters.")

    host, from_url = _extract_host(text)
    host = host.lower().rstrip(".")

    try:
        ip: IPAddress | None = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        if getattr(ip, "scope_id", None):
            raise _invalid("Scoped IPv6 addresses are not accepted.")
        ip = normalise_ip(ip)
        return ParsedTarget(str(ip), ip, "url" if from_url else "ip")

    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise _invalid("That hostname cannot be encoded as a valid domain name.") from exc

    if host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
        raise TargetError("blocked_by_policy", "That hostname is reserved for private networks.")
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise _invalid("That is not a valid public hostname.")
    # "127.1" and "2130706433" are IP addresses to many resolvers, so a purely numeric last
    # label is refused instead of being handed to one.
    if labels[-1].isdigit():
        raise _invalid("That is not a valid public hostname.")
    return ParsedTarget(host, None, "url" if from_url else "hostname")


Resolver = Callable[[str], Awaitable[list[str]]]


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout=5
        )
    except (socket.gaierror, TimeoutError, OSError) as exc:
        raise TargetError("dns_failure", f"Could not resolve {host}.") from exc
    seen: dict[str, None] = {}
    for info in infos:
        seen.setdefault(str(info[4][0]), None)
    return list(seen)


@dataclass(frozen=True)
class ResolvedTarget:
    parsed: ParsedTarget
    resolved_ips: list[str] = field(default_factory=list)
    chosen_ip: IPAddress | None = None  # the address a "fixed" measurement will probe


def check_ip(ip: IPAddress, denylist: Denylist) -> None:
    if not address_is_public(ip):
        raise TargetError(
            "blocked_by_policy", f"{ip} is not a public address and cannot be measured."
        )
    if denylist.blocks_ip(normalise_ip(ip)):
        raise TargetError("blocked_by_policy", f"{ip} is on this deployment's blocklist.")


async def resolve_target(
    parsed: ParsedTarget,
    *,
    family: str,
    denylist: Denylist,
    resolver: Resolver = system_resolver,
) -> ResolvedTarget:
    """Apply the policy and, for hostnames, pin the address that will be probed.

    `family` is "auto", "4" or "6". Every resolved address must be acceptable: a name that
    resolves to one public and one private address is refused rather than partly measured.
    """
    if parsed.ip is not None:
        check_ip(parsed.ip, denylist)
        if family == "4" and parsed.ip.version != 4:
            raise TargetError("family_mismatch", "That is an IPv6 address; IPv4 was requested.")
        if family == "6" and parsed.ip.version != 6:
            raise TargetError("family_mismatch", "That is an IPv4 address; IPv6 was requested.")
        return ResolvedTarget(parsed, [str(parsed.ip)], parsed.ip)

    if denylist.blocks_host(parsed.host):
        raise TargetError("blocked_by_policy", f"{parsed.host} is on this deployment's blocklist.")

    addresses = [
        normalise_ip(ipaddress.ip_address(a.split("%")[0])) for a in await resolver(parsed.host)
    ]
    if not addresses:
        raise TargetError("dns_failure", f"{parsed.host} has no A or AAAA records.")
    for address in addresses:
        check_ip(address, denylist)

    wanted = {"4": (4,), "6": (6,), "auto": (4, 6)}[family]
    for version in wanted:  # IPv4 first when "auto"
        for address in addresses:
            if address.version == version:
                return ResolvedTarget(parsed, [str(a) for a in addresses], address)
    raise TargetError("family_mismatch", f"{parsed.host} has no IPv{family} address.")
