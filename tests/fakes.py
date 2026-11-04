"""Test doubles shared by the test modules."""

from __future__ import annotations

HOSTS = {
    "example.com": ["93.184.216.34"],
    "dual.example.com": ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
    "internal.example.com": ["10.0.0.5"],
    "mixed.example.com": ["93.184.216.34", "192.168.1.10"],
}


async def fake_resolver(host: str) -> list[str]:
    from ipping_backend.security.targets import TargetError

    if host not in HOSTS:
        raise TargetError("dns_failure", f"Could not resolve {host}.")
    return HOSTS[host]
