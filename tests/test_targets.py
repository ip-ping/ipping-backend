from __future__ import annotations

import ipaddress

import pytest

from ipping_backend.security.targets import (
    Denylist,
    TargetError,
    address_is_public,
    parse_target,
    resolve_target,
)

from .fakes import fake_resolver


@pytest.mark.parametrize(
    ("raw", "host", "kind"),
    [
        ("8.8.8.8", "8.8.8.8", "ip"),
        ("2001:4860:4860::8888", "2001:4860:4860::8888", "ip"),
        ("[2001:4860:4860::8888]:443", "2001:4860:4860::8888", "ip"),
        ("example.com", "example.com", "hostname"),
        ("EXAMPLE.com.", "example.com", "hostname"),
        ("example.com:8080", "example.com", "hostname"),
        ("https://example.com/path?token=secret#frag", "example.com", "url"),
        ("https://user:hunter2@example.com:8443/x", "example.com", "url"),
        ("example.com/some/path", "example.com", "url"),
        ("http://8.8.8.8/", "8.8.8.8", "url"),
        ("bücher.example", "xn--bcher-kva.example", "hostname"),
        ("::ffff:8.8.8.8", "8.8.8.8", "ip"),
    ],
)
def test_parse_accepts(raw: str, host: str, kind: str) -> None:
    parsed = parse_target(raw)
    assert (parsed.host, parsed.input_kind) == (host, kind)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "ftp://example.com",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "exa mple.com",
        "localhost",
        "foo.localhost",
        "printer.local",
        "db.internal",
        "singlelabel",
        "127.1",  # shorthand some resolvers expand to 127.0.0.1
        "2130706433",  # decimal form of 127.0.0.1
        "0x7f.0.0.1",
        "fe80::1%eth0",
        "-bad-.example.com",
        "a" * 64 + ".example.com",
        "http://",
        "example.com:99999",
        "x" * 3000,
    ],
)
def test_parse_rejects(raw: str) -> None:
    with pytest.raises(TargetError):
        parse_target(raw)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",  # noqa: S104
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "fd00:ec2::254",
        "ff02::1",
        "64:ff9b::7f00:1",  # NAT64 wrapping 127.0.0.1
        "2002:7f00:1::1",  # 6to4 wrapping 127.0.0.1
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
    ],
)
def test_non_public_addresses_are_refused(ip: str) -> None:
    assert not address_is_public(ipaddress.ip_address(ip))


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_pass(ip: str) -> None:
    assert address_is_public(ipaddress.ip_address(ip))


@pytest.mark.anyio
async def test_hostname_resolving_to_private_address_is_refused() -> None:
    with pytest.raises(TargetError) as exc:
        await resolve_target(
            parse_target("internal.example.com"),
            family="auto",
            denylist=Denylist(),
            resolver=fake_resolver,
        )
    assert exc.value.code == "blocked_by_policy"


@pytest.mark.anyio
async def test_one_private_address_among_public_ones_refuses_the_name() -> None:
    with pytest.raises(TargetError) as exc:
        await resolve_target(
            parse_target("mixed.example.com"),
            family="auto",
            denylist=Denylist(),
            resolver=fake_resolver,
        )
    assert exc.value.code == "blocked_by_policy"


@pytest.mark.anyio
async def test_family_selection_pins_one_address() -> None:
    parsed = parse_target("dual.example.com")
    auto = await resolve_target(parsed, family="auto", denylist=Denylist(), resolver=fake_resolver)
    v6 = await resolve_target(parsed, family="6", denylist=Denylist(), resolver=fake_resolver)
    assert auto.chosen_ip is not None and auto.chosen_ip.version == 4
    assert v6.chosen_ip is not None and v6.chosen_ip.version == 6
    with pytest.raises(TargetError) as exc:
        await resolve_target(
            parse_target("example.com"), family="6", denylist=Denylist(), resolver=fake_resolver
        )
    assert exc.value.code == "family_mismatch"


@pytest.mark.anyio
async def test_operator_denylist_covers_cidrs_and_subdomains() -> None:
    denylist = Denylist.from_entries(["93.184.216.0/24", "blocked.test"])
    with pytest.raises(TargetError):
        await resolve_target(
            parse_target("example.com"), family="auto", denylist=denylist, resolver=fake_resolver
        )
    with pytest.raises(TargetError):
        await resolve_target(
            parse_target("www.blocked.test"),
            family="auto",
            denylist=denylist,
            resolver=fake_resolver,
        )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
