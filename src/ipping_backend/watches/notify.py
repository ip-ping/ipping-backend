"""Telling someone: one HTTP POST to a webhook or an ntfy topic.

The address is chosen by an administrator, but it is still an address the server will connect to from
inside the deployment's network, so it gets the same care as a target: the name is resolved here, every
address must be public (unless the administrator turned that rule off for a webhook on their own
network, which is a normal thing for a self-hosted ntfy), and the connection goes to the address that was
checked, with the original name for the Host header and the certificate. Redirects are not followed.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from ..security.targets import Resolver, address_is_public, system_resolver

log = logging.getLogger(__name__)

TIMEOUT_S = 10.0


class AlertAddressError(ValueError):
    """The address cannot be used; the message says why, in words for an administrator."""


@dataclass(frozen=True)
class Alert:
    event: str  # alert | recovered | test
    watch_id: str
    watch_name: str
    kind: str
    target: str
    status: str
    nodes_ok: int
    nodes_total: int
    median_ms: float | None
    measurement_id: str | None
    at: str

    @property
    def message(self) -> str:
        counts = f"{self.nodes_ok} of {self.nodes_total} nodes get a good answer"
        if self.event == "recovered":
            return f"{self.target} is back ({counts}). Watch: {self.watch_name}."
        if self.event == "test":
            return f"A test alert from the watch {self.watch_name} ({self.target})."
        what = "is not answering" if self.status == "down" else "is degraded"
        return f"{self.target} {what} ({counts}). Watch: {self.watch_name}."


def make_alert(
    event: str,
    watch_id: str,
    watch_name: str,
    kind: str,
    target: str,
    status: str,
    nodes_ok: int,
    nodes_total: int,
    median_ms: float | None,
    measurement_id: str | None,
) -> Alert:
    return Alert(
        event, watch_id, watch_name, kind, target, status, nodes_ok, nodes_total, median_ms,
        measurement_id, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )  # fmt: skip


def _ascii(value: str) -> str:
    return value.encode("ascii", "replace").decode("ascii")[:200]


def render(alert: Alert, fmt: str) -> tuple[dict[str, str], bytes]:
    """The headers and body for the chosen format."""
    if fmt == "ntfy":
        recovered = alert.event == "recovered"
        title = {
            "recovered": f"{alert.target} is back",
            "test": f"Test: {alert.watch_name}",
        }.get(
            alert.event,
            f"{alert.target} is {'not answering' if alert.status == 'down' else 'degraded'}",
        )
        headers = {
            "Title": _ascii(title),
            "Priority": "default" if recovered or alert.event == "test" else "high",
            "Tags": "white_check_mark" if recovered else ("information_source" if alert.event == "test" else "rotating_light"),
            "Content-Type": "text/plain; charset=utf-8",
        }  # fmt: skip
        return headers, alert.message.encode()
    body = {**asdict(alert), "message": alert.message}
    return {"Content-Type": "application/json"}, json.dumps(body).encode()


async def check_address(
    url: str, *, allow_private: bool, resolver: Resolver = system_resolver
) -> tuple[str, list[str]]:
    """Validate an alert address and return its host and the addresses it resolves to."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AlertAddressError("The address must start with http:// or https://.")
    if parts.username or parts.password:
        raise AlertAddressError(
            "Leave credentials out of the address; put a token in the path instead."
        )
    host = parts.hostname
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = await resolver(host)
        except Exception as exc:
            raise AlertAddressError(f"Could not resolve {host}.") from exc
    if not addresses:
        raise AlertAddressError(f"{host} has no address.")
    if not allow_private:
        for address in addresses:
            if not address_is_public(ipaddress.ip_address(address.split("%")[0])):
                raise AlertAddressError(
                    f"{host} is not a public address. Allow private webhooks in the settings if "
                    "this is a service on your own network."
                )
    return host, addresses


async def deliver(
    url: str,
    fmt: str,
    alert: Alert,
    *,
    allow_private: bool,
    resolver: Resolver = system_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str | None:
    """Send the alert. Returns None on success, else a short reason."""
    try:
        host, addresses = await check_address(url, allow_private=allow_private, resolver=resolver)
    except AlertAddressError as exc:
        return str(exc)[:300]
    parts = urlsplit(url.strip())
    pinned = addresses[0].split("%")[0]
    ip_text = f"[{pinned}]" if ":" in pinned else pinned
    port = f":{parts.port}" if parts.port else ""
    target = f"{parts.scheme}://{ip_text}{port}{parts.path or '/'}" + (
        f"?{parts.query}" if parts.query else ""
    )
    headers, body = render(alert, fmt)
    headers["Host"] = f"{host}{port}"
    headers["User-Agent"] = "ipping-alert"
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_S, follow_redirects=False, transport=transport
        ) as client:
            response = await client.post(
                target, content=body, headers=headers, extensions={"sni_hostname": host}
            )
    except httpx.HTTPError as exc:
        return f"{type(exc).__name__}: {exc}"[:300]
    if response.status_code >= 300:
        return f"The server answered {response.status_code}."
    return None
