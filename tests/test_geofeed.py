"""Geofeeds (RFC 8805): reading a feed, finding an address in it, fetching it safely, keeping it, and the result."""

from __future__ import annotations

import gzip
import io
import ipaddress
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from ipping_backend import cli
from ipping_backend.geolocation import geofeed
from ipping_backend.geolocation.geofeed import (
    Entry,
    Geofeeds,
    Table,
    check_host,
    fetch,
    parse,
    read,
    update,
    write,
)

from .conftest import Api
from .test_geoip import _measure

URL = "https://feeds.example.net/geofeed.csv"
OTHER = "https://other.example.org/feed.csv"


def _network(text: str) -> Any:
    return ipaddress.ip_network(text)


# ---------------------------------------------------------------------------------------------
# Reading a feed
# ---------------------------------------------------------------------------------------------


def test_the_lines_of_a_feed_are_read_and_the_rest_is_skipped() -> None:
    text = "\n".join(
        [
            "# a comment, then a blank line",
            "",
            "93.184.216.0/24,DE,DE-HE,Frankfurt am Main,60311",
            '2001:4860:4860::/48,NL,,"Amsterdam, North Holland",',
            "8.8.4.4/32,us,US-CA,Mountain View,",
            "198.51.100.0/24,DE,DE-HE,Documentation range,",  # not a public prefix
            "10.0.0.0/8,DE,,Private,",
            "0.0.0.0/0,US,,Everywhere,",  # a feed that claims the internet is wrong
            "::/0,US,,Everywhere,",
            "93.184.217.5/24,DE,,Host bits set,",  # not a prefix
            "93.184.218.0/24,Germany,,Not a country code,",
            "not a prefix,DE,,x,",
            "93.184.219.0/24,,,,",  # "do not geolocate": says nothing
            "93.184.220.0/24",  # a prefix and nothing else
        ]
    )
    entries, skipped = parse(text, URL)
    found = {str(network): entry for network, entry in entries}
    assert list(found) == [
        "93.184.216.0/24",
        "2001:4860:4860::/48",
        "8.8.4.4/32",
    ]
    assert found["93.184.216.0/24"] == Entry("DE", "DE-HE", "Frankfurt am Main", URL)
    assert (
        found["2001:4860:4860::/48"].city == "Amsterdam, North Holland"
    )  # quoted: the comma is part of the name
    assert found["8.8.4.4/32"].country == "US"  # lower case is accepted
    assert skipped == 9


def test_a_bad_region_does_not_make_the_country_and_city_wrong() -> None:
    entries, skipped = parse("93.184.216.0/24,DE,Hessen,Frankfurt,", URL)
    assert skipped == 0 and entries[0][1] == Entry("DE", "", "Frankfurt", URL)


def test_a_feed_with_a_byte_order_mark_and_control_characters_is_cleaned() -> None:
    text = fetch(URL, lambda _: io.BytesIO(b"\xef\xbb\xbf93.184.216.0/24,DE,,Fran\x07kfurt,\n"))
    entries, _ = parse(text, URL)
    assert entries[0][1].city == ""  # control characters make the name untrusted, so it is dropped
    assert entries[0][1].country == "DE"


# ---------------------------------------------------------------------------------------------
# Finding an address
# ---------------------------------------------------------------------------------------------


def test_the_longest_prefix_that_holds_the_address_wins() -> None:
    table = Table()
    for prefix, city in [
        ("93.184.0.0/16", "Berlin"),
        ("93.184.216.0/24", "Frankfurt"),
        ("93.184.216.128/25", "Munich"),
        ("2001:db8::/32", "Nowhere"),
        ("2a00:1450::/32", "Dublin"),
        ("2a00:1450:4001::/48", "Paris"),
    ]:
        table.add(_network(prefix), Entry("XX", "", city, URL))
    cases = {
        "93.184.216.34": ("Frankfurt", "93.184.216.0/24"),
        "93.184.216.200": ("Munich", "93.184.216.128/25"),
        "93.184.1.1": ("Berlin", "93.184.0.0/16"),
        "2a00:1450:4001::5": ("Paris", "2a00:1450:4001::/48"),
        "2a00:1450:4002::5": ("Dublin", "2a00:1450::/32"),
    }
    for ip, (city, prefix) in cases.items():
        found = table.lookup(ip)
        assert found is not None and (found[0].city, found[1]) == (city, prefix), ip
    assert table.lookup("8.8.8.8") is None
    assert table.lookup("2606:4700::1") is None
    assert table.lookup("not an address") is None
    assert table.size == 6


# ---------------------------------------------------------------------------------------------
# Keeping it
# ---------------------------------------------------------------------------------------------


def test_what_is_kept_comes_back_the_same(tmp_path: Path) -> None:
    path = tmp_path / "nested" / geofeed.FILE_NAME
    rows = [
        ("93.184.216.0/24", Entry("BR", "BR-SP", "São Paulo", URL)),
        ("2a00:1450::/32", Entry("NL", "", "Amsterdam, North Holland", OTHER)),
    ]
    write(path, rows)
    assert read(path) == rows
    assert read(tmp_path / "missing.csv.gz") == []
    (tmp_path / "broken.csv.gz").write_bytes(b"not gzip")
    assert read(tmp_path / "broken.csv.gz") == []
    assert list(path.parent.glob("*.part")) == []


def _opener(feeds: dict[str, str | Exception]) -> Any:
    def opener(url: str) -> io.BytesIO:
        body = feeds[url]
        if isinstance(body, Exception):
            raise body
        return io.BytesIO(body.encode())

    return opener


def test_a_feed_that_fails_keeps_what_it_had_and_one_no_longer_listed_goes(tmp_path: Path) -> None:
    path = tmp_path / geofeed.FILE_NAME
    good = "93.184.216.0/24,DE,,Frankfurt am Main,\n8.8.4.0/24,US,,Mountain View,\n"
    other = "2a00:1450::/32,NL,,Amsterdam,\n"
    first = update(path, [URL, OTHER], _opener({URL: good, OTHER: other}))
    assert [(r.url, r.entries, r.error) for r in first] == [(URL, 2, None), (OTHER, 1, None)]
    assert len(read(path)) == 3

    # The first feed is down this time; the second still works and gains a line
    second = update(
        path,
        [URL, OTHER],
        _opener(
            {URL: urllib.error.URLError("down"), OTHER: other + "2a00:1451::/32,NL,,Amsterdam,\n"}
        ),
    )
    assert second[0].error is not None and second[0].kept_previous and second[0].entries == 2
    assert second[1].error is None and second[1].entries == 2
    assert len(read(path)) == 4

    # A feed that answers with nothing usable counts as down, and one that is dropped from the list is gone
    third = update(path, [URL], _opener({URL: "# nothing here\n"}))
    assert third[0].kept_previous and "no usable lines" in (third[0].error or "")
    assert {entry.source for _, entry in read(path)} == {URL}

    # Listing a feed twice fetches it once
    again = update(tmp_path / "again.csv.gz", [URL, URL], _opener({URL: good}))
    assert len(again) == 1


def test_all_feeds_together_have_a_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(geofeed, "MAX_ENTRIES_TOTAL", 2)
    with pytest.raises(ValueError, match="list fewer feeds"):
        update(
            tmp_path / geofeed.FILE_NAME,
            [URL],
            _opener({URL: "8.8.4.0/24,US,,A,\n8.8.8.0/24,US,,B,\n1.1.1.0/24,AU,,C,\n"}),
        )


def test_a_feed_is_bounded_and_may_arrive_compressed() -> None:
    packed = gzip.compress(b"93.184.216.0/24,DE,,Frankfurt am Main,\n")
    assert "Frankfurt" in fetch(URL, lambda _: io.BytesIO(packed))
    with pytest.raises(ValueError, match="longer than"):
        fetch(URL, lambda _: io.BytesIO(b"x" * (geofeed.MAX_BYTES + 1)))
    with pytest.raises(ValueError, match="when unpacked"):
        fetch(URL, lambda _: io.BytesIO(gzip.compress(b"x" * (geofeed.MAX_BYTES + 1))))
    with pytest.raises(ValueError, match="UTF-8"):
        fetch(URL, lambda _: io.BytesIO(b"\xff\xfe\xfa"))


# ---------------------------------------------------------------------------------------------
# Fetching only from where it is safe
# ---------------------------------------------------------------------------------------------


def test_only_https_to_a_public_host_is_fetched() -> None:
    for url in (
        "http://feeds.example.net/geofeed.csv",
        "ftp://x.example/geofeed.csv",
        "file:///etc/passwd",
        "https:///x",
    ):
        with pytest.raises(ValueError, match="only https"):
            geofeed._open(url)
    with pytest.raises(ValueError, match="not a public host"):
        check_host("localhost")
    with pytest.raises(ValueError, match="not a public host"):
        geofeed._open("https://127.0.0.1/geofeed.csv")


def test_a_redirect_to_anything_but_https_is_refused() -> None:
    handler = geofeed._HttpsOnly()
    request = urllib.request.Request(URL)
    with pytest.raises(urllib.error.URLError, match="other than https"):
        handler.redirect_request(
            request, io.BytesIO(), 302, "Found", {}, "http://feeds.example.net/x.csv"
        )  # type: ignore[arg-type]
    with pytest.raises(urllib.error.URLError, match="other than https"):
        handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, "file:///etc/passwd")  # type: ignore[arg-type]
    followed = handler.redirect_request(
        request, io.BytesIO(), 302, "Found", {}, "https://elsewhere.example/x.csv"
    )  # type: ignore[arg-type]
    assert followed is not None and followed.full_url == "https://elsewhere.example/x.csv"


# ---------------------------------------------------------------------------------------------
# Using it
# ---------------------------------------------------------------------------------------------


def test_the_file_is_read_again_when_it_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / geofeed.FILE_NAME
    feeds = Geofeeds(path)
    assert feeds.entries == 0 and feeds.lookup("93.184.216.34") is None  # no file yet
    write(path, [("93.184.216.0/24", Entry("DE", "", "Frankfurt am Main", URL))])
    found = feeds.lookup("93.184.216.34")
    assert found is not None and found[0].city == "Frankfurt am Main" and feeds.entries == 1
    write(
        path,
        [
            ("93.184.216.0/24", Entry("NL", "", "Amsterdam", URL)),
            ("8.8.4.0/24", Entry("US", "", "Mountain View", URL)),
        ],
    )
    found = feeds.lookup("93.184.216.34")
    assert found is not None and found[0].city == "Amsterdam" and feeds.entries == 2
    path.unlink()
    assert feeds.lookup("93.184.216.34") is None
    assert Geofeeds(None).entries == 0


def test_a_city_is_found_in_the_gazetteer_or_left_unchecked(tmp_path: Path) -> None:
    feeds = Geofeeds(None)
    cases = {
        ("DE", "Frankfurt am Main"): "Frankfurt am Main",
        ("DE", "Frankfurt"): "Frankfurt am Main",  # the biggest city that starts with the name
        ("NL", "amsterdam"): "Amsterdam",
        ("DE", "Falkenstein"): None,  # a town the gazetteer does not have
        ("DE", ""): None,  # a country is no place to check against
        ("", "Frankfurt"): None,
    }
    for (country, city), name in cases.items():
        place = feeds.place(Entry(country, "", city, URL))
        assert (place.name if place else None) == name, (country, city)


# ---------------------------------------------------------------------------------------------
# On a result
# ---------------------------------------------------------------------------------------------


def _with_feed(api: Api, tmp_path: Path, country: str, city: str) -> None:
    path = tmp_path / geofeed.FILE_NAME
    write(path, [("93.184.216.0/24", Entry(country, "", city, URL))])
    api.client.app.state.geofeeds = Geofeeds(path)  # type: ignore[attr-defined]


def test_a_result_has_no_geofeed_unless_a_feed_is_set_up(api: Api) -> None:
    assert _measure(api, verified=True)["geofeed"] is None


def test_a_feed_that_does_not_cover_the_address_adds_nothing(api: Api, tmp_path: Path) -> None:
    path = tmp_path / geofeed.FILE_NAME
    write(path, [("8.8.4.0/24", Entry("US", "", "Mountain View", URL))])
    api.client.app.state.geofeeds = Geofeeds(path)  # type: ignore[attr-defined]
    assert _measure(api, verified=True)["geofeed"] is None


def test_a_feed_that_names_a_place_the_measurements_rule_out_says_so(
    api: Api, tmp_path: Path
) -> None:
    _with_feed(api, tmp_path, "JP", "Tokyo")
    result = _measure(api, verified=True)
    feed = result["geofeed"]
    assert feed["verdict"] == "ruled_out" and feed["place"]["name"] == "Tokyo"
    assert (
        feed["source"] == "Geofeed from feeds.example.net" and feed["prefix"] == "93.184.216.0/24"
    )
    assert feed["claim"] == "Tokyo, JP"
    assert feed["ruled_out_by"]["max_distance_km"] < feed["ruled_out_by"]["distance_km"]
    # the estimate is what it always was: a feed does not move it
    assert result["status"] == "estimated" and result["likely"]["country"] != "JP"


def test_a_feed_that_agrees_with_the_measurements_is_consistent(api: Api, tmp_path: Path) -> None:
    _with_feed(api, tmp_path, "NL", "Amsterdam")
    feed = _measure(api, verified=True)["geofeed"]
    assert feed["verdict"] == "consistent" and feed["ruled_out_by"] is None
    assert feed["distance_to_estimate_km"] < 600


def test_a_city_the_gazetteer_cannot_place_is_shown_as_the_feed_wrote_it(
    api: Api, tmp_path: Path
) -> None:
    _with_feed(api, tmp_path, "DE", "Falkenstein")
    feed = _measure(api, verified=True)["geofeed"]
    assert feed["verdict"] == "not_checked" and feed["place"] is None
    assert feed["claim"] == "Falkenstein, DE"


def test_nodes_with_unverified_positions_rule_nothing_out_of_a_feed_either(
    api: Api, tmp_path: Path
) -> None:
    _with_feed(api, tmp_path, "JP", "Tokyo")
    feed = _measure(api, verified=False)["geofeed"]
    assert (
        feed["verdict"] == "not_checked"
        and feed["ruled_out_by"] is None
        and feed["place"]["name"] == "Tokyo"
    )


def test_the_admin_sees_how_many_prefixes_are_loaded(api: Api, tmp_path: Path) -> None:
    api.become_admin()
    assert api.get("/api/v1/admin/status").json()["geoip"]["geofeed_entries"] == 0
    _with_feed(api, tmp_path, "NL", "Amsterdam")
    assert api.get("/api/v1/admin/status").json()["geoip"]["geofeed_entries"] == 1


# ---------------------------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------------------------


def test_the_command_reports_each_feed_and_fails_only_when_nothing_worked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("IPPING_GEOIP_DIR", str(tmp_path))
    monkeypatch.setenv("IPPING_GEOFEED_URLS", f"{URL}, {OTHER}")
    seen: dict[str, Any] = {}

    def fake(path: Path, urls: list[str]) -> list[geofeed.FeedResult]:
        seen.update(path=path, urls=urls)
        return [geofeed.FeedResult(URL, 12, 3), geofeed.FeedResult(OTHER, 0, 0, "timed out")]

    monkeypatch.setattr(geofeed, "update", fake)
    cli.main(["geofeed-update"])
    out = capsys.readouterr().out
    assert seen["urls"] == [URL, OTHER] and seen["path"] == tmp_path / geofeed.FILE_NAME
    assert "feeds.example.net: 12 prefixes, 3 lines skipped" in out
    assert "other.example.org: FAILED, timed out" in out

    monkeypatch.setattr(
        geofeed, "update", lambda path, urls: [geofeed.FeedResult(URL, 0, 0, "timed out")]
    )
    with pytest.raises(SystemExit) as caught:
        cli.main(["geofeed-update", "--url", URL])
    assert caught.value.code == 1

    monkeypatch.delenv("IPPING_GEOFEED_URLS")
    with pytest.raises(SystemExit) as nothing:
        cli.main(["geofeed-update"])
    assert nothing.value.code == 2
