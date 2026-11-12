# Changelog

Progress log for `ipping-backend`, newest first. Each entry is one small, tested step.

## v0.15.0 (2026-11-12)

Sponsored nodes. Database migration `0014`; one new field in the API, nothing removed.

- **A node can be sponsored.** `sponsored` is a flag on a node and on an enrollment token (the node that enrolls with a token takes its flag, as it takes
  its tier; an administrator can change it with `PATCH /admin/nodes/{id}`). It is said on every node of `GET /nodes` and on the `node` of every task
  of a measurement, so that the web interface can make the provider's name stand out where the node is shown. It is only a flag: it is independent of the tier
  (a volunteer's node can be sponsored) and changes nothing about how the node is chosen, limited, trusted or used in a location estimate. A result keeps what the
  flag was when the measurement began, like the node's place and provider, and a result stored before this reads as not sponsored. The migration adds the
  column to `nodes` and `enrollment_tokens` with everything existing not sponsored; it was run forward and back on SQLite and PostgreSQL with rows in both tables.
- **A `null` for a flag no longer crashes `PATCH /admin/nodes/{id}`.** `{"tier": null}` and `{"location_verified": null}` were an unhandled database error
  (`NOT NULL constraint failed`, a 500 on a real server); found while adding this flag, which has the same shape. A flag has no "unset", so `null` now leaves it as it is, for all three.
- Tests: `tests/test_sponsored.py` (7: nothing is sponsored by default, token and administrator set it, `null`/omitted leave it, it is independent of the tier,
  a result keeps what it was, an old result reads as not sponsored, the migration), each mutant caught (the result forgetting the flag, the token not giving it to the node, no guard against `null`).
  590 pass on SQLite (12 need PostgreSQL) and 602 on PostgreSQL. The interface is the next step, in `ipping`.

## v0.14.0 (2026-11-12)

The IP data vote measured, and mended where the measurement showed it wrong. No database change (still `0013`), no change to the API.

Released and verified: the files match `SHA256SUMS`, the wheel, the lockfile and the contracts carry attestations (and a wheel altered after release is
refused), `ipping-backend:0.14.0` and `0.14` are on Docker Hub for amd64 and arm64 (one digest), the wheel holds `ipdata/measured.py`, the released contract says
0.14.0 and its wheel's hash is the one `ipping` v0.17.1 pins. **The released image was run with nothing configured** (an empty data volume, online allowed, the released
`ipping:0.17.1` in front of it): it downloaded the six sources that need no account by itself, all `ready`, and voted real addresses: `1.1.1.1` is Australia at 99% with
IPtoASN's `US` outvoted (it was the case that showed the weakness), `8.8.8.8` is Mountain View at 99% for the country with all six agreeing. Run on PostgreSQL: 595 tests. Not run
against a real account: MaxMind, IPinfo Lite and IP2Location LITE, so their reliabilities are the capped priors described above.

- **The sources were measured.** `scripts/collect_anchors.py` writes `calibration/atlas-anchors.json.gz` (1,096 RIPE Atlas anchors with an IPv4
  and often an IPv6 address, the country and position their hosts registered, and the AS number), and `scripts/evaluate_ipdata.py` asks every
  source that has a database about all 2,112 addresses. As for the location model, the anchors are split in two by a fixed rule: the numbers in
  the new, generated `ipdata/measured.py` come from the calibration half and are quoted from the test half. The old numbers become
  `consensus.PRIORS` and are used where nothing was measured. The priors were too high: the country was right 93% of the time for DB-IP and IPLocate
  (priors 96%), 84% for ipapi.is (95%), and 77 to 80% for IPtoASN, IPdeny and ip2region (93 to 94%).
- **Sources that are wrong together are one witness.** Of the addresses where two sources were both wrong about the country, they gave the same
  wrong one 41 to 94% of the time, and 81% for IPtoASN and IPdeny, which say where an address block is *registered* (a German data centre on an American
  block is `US` to both). The vote assumed independent mistakes, so the two registries out-voted the databases that were right. They are now one
  witness, with the more reliable of the two (`consensus.FAMILIES`); both are still listed with what they said. Putting the location databases in a
  family too made the answer worse, so it was not done. This is the "IPtoASN is a weak voter on the country" item, mended by the data and not by
  switching the source off: it is still the AS number's best source.
- **The answer is the value with the highest percentage.** It used to be the value with the most votes, and the percentage was worked out
  after. With sources of unequal reliability the two disagree: three weak sources against two strong ones answer the three, and the percentage
  shown beside that answer is low because it rates the two higher. Tried on the held-out half with the measured reliabilities and the old
  ordering, the country answers that the vote itself rated below 70% (32 of them) were right 12% of the time; with the new rule the 23 that
  are rated below 70% are right 87% of the time. With sources of equal reliability it is still the plain majority.
- **A source that could not be measured** (MaxMind, IPinfo, IP2Location: no account was available) is trusted no more than the best source that was,
  for a field that was measured at all. Their claimed 98% over a measured 94% would have out-weighed sources nobody had doubted.
- **The effect, on the test half** (1,062 addresses): the country is right 93.2% of the time (the v0.13.0 vote: 90.8%; DB-IP alone: 93.0%),
  the position within 60 km 74.0% (71.6%); the Brier score of the percentages 0.065 for the country (0.090) and 0.181 for the position (0.194). Of
  the 994 country answers shown at 97 to 99%, 94.6% were right: the percentages are still a little confident.
- **What it does not say.** Anchors are well-run hosts in data centres and vendors know them, so every figure is an upper bound. The AS number is
  measured and shown but **not fitted**: where three databases agree on it and the anchor registered another, the databases are usually right (the
  anchor names its host's AS; 7% of them), so the anchors are not ground truth for it. Region, city, postal code, time zone and organization have
  no ground truth in them and keep their priors. MaxMind, IPinfo and IP2Location were never run against a real account.
- Tests: the sources that share mistakes (with the rule taken away, the registries win), the answer being the highest percentage (400 random
  ballots), the cap on unmeasured sources, the generated table, the anchor snapshot and the scoring of the evaluation script; each of the three
  rules was taken out in turn and a test failed.

## v0.13.0 (2026-11-11)

Every IP data source is a database that is downloaded and served from disk, and all nine are treated the same. No database change (still `0013`).

Released and verified: the files match `SHA256SUMS`, the wheel, the lockfile and the contracts carry attestations (and a file altered after release is
refused), `ipping-backend:0.13.0` is on Docker Hub for amd64 and arm64, the contract is the one `ipping` v0.16.0 was built against, and `ipping`
v0.16.0 pins this release. **The released image was run with nothing configured** (an empty data volume, online allowed, the released `ipping:0.16.0`
beside it): it downloaded DB-IP, IPLocate, ipapi.is, IPtoASN, IPdeny and both ip2region files by itself in about a minute (440 MB), all six sources
were `ready`, 8.8.8.8 and 1.1.1.1 were voted with the real data (Mountain View 99% on the country, South Brisbane for Cloudflare's anycast address),
and the admin API had no `maxmind` block. A made-up IP2Location token saved in the page made the backend ask the **real IP2Location server**, which
answered `NO PERMISSION` with status 200: it was recognised, shown as "IP2Location did not give the file (NO PERMISSION)", and the token appeared in no
log line, status file or API answer. Not run against a real account: MaxMind, IPinfo Lite and the accepted-token path of IP2Location LITE.

- **No source's API is called any more.** IPinfo, IPLocate, ipapi.is and IP2Location were asked over HTTPS on every lookup (v0.12.0). They are
  now their free downloadable databases, kept on disk like the other five: **IPinfo Lite** (`ipinfo_lite.mmdb`, a free account's token, checked
  against IPinfo's published checksum, which does not count against its ten downloads a day), **IPLocate** (its free country and ASN `.mmdb` files
  on GitHub, no account), **ipapi.is** (its free geolocation CSV zips, no account) and **IP2Location LITE** (DB11 CSV zips, a free account's
  download token, downloaded once a month as IP2Location asks). A lookup reads files and never touches the network (a test makes any connection
  an error). The online client, its 24 hour cache, its rate-limit pause and the `online` kind are gone.
- **MaxMind is one source among nine.** Its separate updater, status file, settings (`maxmind_editions`, `maxmind_auto_update`,
  `maxmind_update_hours`) and its block in the admin API are gone: the same downloader fetches City and ASN (a free `Last-Modified` question first,
  the archive's SHA-256 checked, an hour after a failure and six after a refused key, as before), its account ID and license key are that source's
  credentials like any other source's key, and `IPPING_MAXMIND_ACCOUNT_ID` / `IPPING_MAXMIND_LICENSE_KEY` still win and are shown as coming from
  the environment. An account saved by an earlier version is read from where it was and moved with the other keys the first time any credential
  changes. Files that an earlier version downloaded are not downloaded again: their age says whether MaxMind has something newer.
- **Nothing prefers a source any more.** The cross-check of a place against the measurements, the networks and places of a traceroute and a node's
  own details used "the newest `GeoLite2` file, else DB-IP's": they now use the same vote as the lookup API, so what any source says counts
  everywhere. `GeoIp` only finds the MaxMind-format files now (the IPLocate and IPinfo files, which have `country` and `asn` in their names and a layout
  of their own, are never taken for MaxMind's).
- **ipapi.is's ranges overlap**, which a table that answers "the last range starting at or before this address" cannot cope with (`1.0.4.0-1.0.4.255`
  and `1.0.4.0-1.0.7.255` are both in the file, and 24 607 IPv4 ranges overlap only in part). The table is built flat, the narrowest range winning
  (`ipdata/places.py`), with each place stored once. Checked against a brute-force scan of the real file for 3 287 addresses (no difference), against
  a random brute-force oracle in the tests (and the tests fail when the rule is broken, which was tried), and with the file in order of start
  or not. Built in 17 seconds from the real 66 MB and 8 MB zips, a table of 109 MB, 120 MB peak memory.
- **The admin API changes shape** (the web interface of ipping v0.16.0 follows it): `GET /api/v1/admin/ip-data` has no `maxmind` block, no
  `databases`, `source` or `attribution`; every source in `sources` has `credentials` (with `from_environment`), `files`, `running`, `last_attempt`,
  `last_success`, `next_check` and `error`, and there is a top-level `directory_configured`. `PUT` takes `data_update_hours` and `sources: {id: {enabled,
  credentials}}` only (MaxMind's are `account_id` and `license_key` there). `POST .../update` is now **check for newer files** (a source is downloaded only
  if it has something newer, so repeated clicks cost nothing and cannot use up MaxMind's 30 downloads a day) and answers 409 `nothing_to_update` when no source that
  is on can be downloaded yet. In an answer, `kind` is gone and `rate_limited` is not a status any more.
- **The dashboard's status** (`GET /api/v1/admin/status`, `geoip.source`) names the sources that know places or networks when there are one or two, and how many (`7 IP data sources`) when there are more.
- **A token cannot reach a log.** IPinfo's and IP2Location's downloads carry the token in the URL, and httpx writes the URL of every request
  at INFO, which is the server's level: `httpx` and `httpcore` are now kept at WARNING (tested with a control that shows the token *would* be
  logged otherwise), and failures are reported by what kind they were, never by the library's text. **This also applies to v0.12.0**, whose
  four online sources sent their optional keys as URL parameters: a server that ran v0.12.0 with such a key set has it in its logs, and the key
  should be replaced.
- **Tested** (571 tests, 583 on PostgreSQL; the IP data tests were rewritten around local files for this): the downloaders against pretend servers (IPinfo's checksum and redirect, the
  token reaching only IPinfo, IP2Location's refusal as text with status 200, its once-a-month rule, a download that does not match its checksum or is
  the wrong kind replacing nothing, one of two files failing without losing the other, nothing left behind, offline, a source that needs a key not being
  downloaded without it), the readers of the real layouts of IPLocate and IPinfo, the place tables, and the admin API. **Run against the real
  servers**: DB-IP, IPLocate, ipapi.is, IPtoASN, IPdeny and ip2region downloaded, checked and built in 41 seconds (142 MB peak memory, 425 MB on disk),
  and voted on for 8.8.8.8, 1.1.1.1, 114.114.114.114, `2001:4860:4860::8888` and 223.5.5.5 in 1 to 6 ms each. **Not run against the real
  service**: MaxMind, IPinfo Lite and IP2Location LITE, which need an account (no account was used): IPinfo's and IP2Location's request shapes
  are from their documentation, and IPinfo's file layout from its published sample.

## v0.12.0 (2026-11-11)

Nine IP data sources, each with a switch, and a majority vote with a percentage for every field. No database change (still `0013`: the
switches and keys are rows of `app_settings`).

Released and verified: the files match `SHA256SUMS` and carry attestations, `ipping-backend:0.12.0` is on Docker Hub and the `ipping` v0.15.0 release
pins it. **The released image was run with nothing configured** (all sources on, online allowed, an empty data volume): it downloaded DB-IP,
IPtoASN, IPdeny and both ip2region files by itself in 25 seconds, and eight sources voted for 8.8.8.8 (the real IPinfo, IPLocate, ipapi.is and
IP2Location answered; MaxMind said it needs an account), 99% on the country, 97% on the city.

- **Nine sources** beside the old single MaxMind: **DB-IP Lite**, **IPtoASN**, **IPdeny** and **ip2region** (databases the backend downloads and
  keeps up to date itself, no account needed) and **IPinfo**, **IPLocate**, **ipapi.is** and **IP2Location** (online; each works without a key at
  a low daily limit and takes an optional key or token). **All are on by default**, each can be turned off in the admin page, and the keys are
  write-only (the API says only that one is set). `GET /api/v1/admin/ip-data` lists every source with its state and files, `PUT` takes
  `sources: {id: {enabled, credentials}}` and `data_update_hours`, "Update now" fetches every database that is on, and turning a source on
  starts its download. `IPPING_IPDATA_OFFLINE=true` stops every outgoing request of this module (no lookups, no downloads).
- **The vote** (`ipdata/consensus.py`): every field is answered by a majority of the sources that have something for it, and **answered
  whenever one source does** (one of nine with data still gives that data). Only sources that agree on the country vote on region, city and
  position, only those that agree on the AS number vote on the organization; places and organizations are compared without accents, case and
  suffixes (`GOOGLE - Google LLC`, `Google LLC` and `Google Cloud` are one organization; found with the real IPtoASN data, whose names
  are written that way), positions within 60 km are one vote (the median is answered), a tie goes to the more reliable sources. **The percentage** is the
  odds that the winner is right from each source's reliability for that kind of data (priors: they are not measured on a deployment, and
  the README says so): `odds(value) / (1 + sum of the odds of every value given)`, shown from 1 to 99. One source of 80% gives 80, two agreeing 94,
  two disagreeing about 44 each, three against one 93.
- **The answer** (`GET /api/v1/ip/{address}`, and `info` of `GET /api/v1/measurements/{id}/addresses`) keeps every field it had and gains
  `confidence` (a percentage for each answered field) and `sources` (every source, with what it said, whether it voted with the winner or against
  it for each field, or why it did not answer: `disabled`, `needs_setup`, `waiting`, `offline`, `rate_limited`, `error`, `empty`). `source` and
  `attribution` name only the sources that contributed. A 503 now means no source can answer at all.
- **What is read how**: MaxMind and DB-IP as `.mmdb` files through `GeoIp` (which now keeps them apart by flavour and honours the switches);
  IPtoASN and IPdeny as SQLite range tables built at download (`ipdata/ranges.py`, a few MB, no memory); ip2region's `.xdb` files with a small
  reader of the format (`ipdata/xdb.py`, memory-mapped; checked against the real IPv4 and IPv6 files). Downloads are checked (must open, be the
  right kind, answer for 8.8.8.8) and replaced atomically, with a lock and a status file shared by the processes; DB-IP's file names, IPtoASN
  and IPdeny's and GitHub's HEAD `ETag` / `Last-Modified` say whether there is anything newer.
- **What does not change**: the location estimate's cross-check, the traceroute networks and the node autofill keep using the local
  MaxMind-format database (now honouring the switches); the online services are asked only by the lookup API and a measurement's addresses.
- **Tested** (538 tests, 61 of them new): the cleaning of every source's words, the voting rules and the percentage, the range tables and
  ip2region with files built in the tests, the real response shapes of the four online services (captured for 8.8.8.8) and the keyed shapes,
  keys never in an answer, rate limiting, caching, failure in four ways at once with one source still answering, the downloader against
  pretend servers (checks, atomic replacement, nothing downloaded twice, last month's DB-IP file, bad data replaces nothing, offline) and the
  admin API (switches, write-only keys, refusals, "Update now" to files to lookups). **Also run against the real services once**: the four
  databases downloaded in 9 seconds and checked, and eight sources voted for 8.8.8.8, 1.1.1.1 and 114.114.114.114 (the real MaxMind was
  not tried: no account).

## v0.11.0 (2026-11-11)

Released and verified: the files match `SHA256SUMS` and carry attestations, and `ipping-backend:0.11.0` is on Docker Hub. The
`ipping` v0.13.0 release pins it (`release-manifest.json` names backend v0.11.0, schema `0013`).

The web interface's languages become settings. No database change (still `0013`: settings are rows of `app_settings`).

- **Three new settings** an administrator changes in the admin page (or `PATCH /api/v1/admin/settings`): `languages_enabled` (which of
  `en zh es fr de pt ru ja ko` a visitor can choose; all by default), `default_language` (`en`) and `language_auto_detect` (`true`: the
  interface follows the browser's language until the visitor picks one). They must hold together: at least one language on and the
  default among them; a change that breaks that is refused with 422 `invalid_settings` before anything is written.
- **`GET /api/v1/config`**, for anyone with no session: `{"languages": {"enabled": [...], "default": "en", "auto_detect": true}}`, which
  is what the interface asks before it draws itself.
- Tests: four (the defaults and the public answer, a choice kept once each in the interface's order, contradictory settings refused and
  nothing written, the choice in the audit trail).

## v0.10.0 (2026-11-11)

Released and verified: the files match `SHA256SUMS` and carry attestations, a PostgreSQL database migrated by the released v0.9.0 image
(with a used enrollment key in it) upgrades to `0013` with the released v0.10.0 image and the key counts one use, and the released image
with two released node v0.8.0 containers enrolled both with **one reusable key and no name** (called `Sydney, AU` and `Frankfurt, DE`,
`custom_name` empty, the key `reusable` with `uses` 2), approved them, ran a measurement on both, removed one by revoking it (gone from
the list) and answered 404 to a key rotation.

The location estimate is **not strict any more**: it always gives an estimate, with a percentage for how accurate answers like it
have been. The database migrates to `0013` (reusable enrollment keys); the estimator model is now `multilateration-5`, so stored estimates are worked out again.

### What changed
- **An answer whenever a node with a position took part.** It used to decline with fewer than 3 such nodes, with a closest node
  over 40 ms away, and when nodes contradicted each other (36% of the random fleets of the calibration got no answer). Now those are
  warnings: "Only 2 node(s)...", "The closest node still measured 90 ms, which leaves most of the world possible: treat this as a
  guess", and, for nodes that disagree, the old explanation (anycast, routing, bad data). The only answer without a place is a
  measurement that no node with a position took part in (`insufficient_evidence`, with the reason). `inconclusive` is no longer
  given; the field stays in the contract.
- **Nodes that disagree** (`conflicting: true`): the estimate is made from the nearest node and, in order of round trip, every node
  that does not contradict one already taken, instead of refusing. It is never `strong`. Real unicast fleets never trigger it;
  99% to 100% of the anycast fleets with 16 or more nodes do. Its accuracy is the table's figure for a closest node as far away
  as the nodes disagree (at fibre speed), not for the nearest node, which would say 99% for an address that nodes 8,000 km apart
  are both a few ms from: a cautious stand-in, said as such in `accuracy.basis`.
- **A tie goes to the city nearest the nearest node** (a ring of equally good places around one node used to be settled by the order
  of the list), and a node with no city of 100,000 inside its limit falls back to the city nearest it.
- **`accuracy`** on every estimate with a place: `percent` (1 to 99, never certainty) is the share of answers, on real targets of
  known position, that were within `radius_km` (500) when the closest node was about this far away, `by_radius` the same for 100,
  300, 500, 1,000 and 2,000 km, and `basis` says in words that it is a rate for answers like this one and not a probability for
  this address (and that, for disagreeing nodes, the real figure is lower). `geolocation/accuracy.py` holds the table; it is
  generated: `uv run python scripts/evaluate_location.py --fit-accuracy` fits it on the calibration half of the RIPE Atlas data,
  never lets it rise as the closest node gets further, and reports how it does on the test half (Brier 0.096; stated and true
  share within 7 points overall). Re-fit it whenever the model changes.
- `scripts/evaluate_location.py` runs fleets of 1, 2, 4, 8, 16 and 32 and reports `conflicting` for the anycast check; results for
  the new model are in `calibration/` (`result-test-multilateration-5.txt`, `result-anycast-multilateration-5.txt`,
  `accuracy-fit-multilateration-5.txt`). The README section "Estimated network location" is rewritten around them.

- **`GET /api/v1/measurements/{id}/addresses`**: what the IP databases say about each address the nodes tested (place, network, AS;
  most tested first, at most 50, with the number of nodes), `databases_loaded` and a null `info` when no database is installed. It
  belongs to the measurement: whoever may read the measurement may read it, whatever the lookup API's access setting says, because it
  only ever covers the addresses that measurement tested (no free lookups). A private address a node reports is never kept, so
  never listed. The web interface's measurement page uses it.
- **The README says what the design now is**: a best guess, always given, rather than an answer held back until it is safe: what
  that costs (85% of the answers are `low`, median 1,500 km) and what it buys, with the accuracy percentage as the reader's guide.

- **Enrollment keys can be used again.** A token made `reusable` enrolls any number of nodes until it expires or is revoked (each new
  node still waits for approval); `uses` counts them and `node_id` is the latest. Single-use stays the default. A key may now be good
  for up to 30 days (it was 24 hours). Migration `0013` adds `enrollment_tokens.reusable` and `uses` (existing tokens stay
  single-use; the ones already used count one use).
- **Node credentials cannot be rotated any more**: `POST /admin/nodes/{id}/rotate-credential` is gone. To change a node's key, revoke
  the node and enroll it again.
- **A revoked node is removed**, not kept as a "revoked" row: it leaves the admin list at once, its credential is refused (nothing
  to find), and the results it produced keep the node as it was. Rows revoked before this change are hidden from the list and swept
  by the maintenance run. The audit trail keeps `node.revoked` with the node's name.

- **A node needs no name.** `name` may be left out or empty when a node enrolls, and may be cleared by an administrator: the node is
  then called `City, CC - Provider · ASn` (for example `Sydney, AU - OVH SAS · AS16276`; the place falls back to the region, then
  the country; the network falls back to whichever of the provider and the AS number is known; with nothing known, `Node abc123`).
  It follows the data as the IP databases fill the place and the provider in. `name` in every answer (the public list, the admin
  list, a measurement's nodes, the text output, estimates, messages) is the name a node is shown by; the admin list also has
  `custom_name`, the one an administrator gave or the node enrolled with (empty: the default). Nodes that enrolled with a name keep
  it. `nodes/naming.py` is the one place the default is made.
- A test that looked for the typed text `pw` in a whole measurement could fail on a random id that happened to spell it; it now looks
  at what was typed.

### Tests
- Five tests for default names (the fallbacks, enrolling with no name, an empty one and a blank one, a given name kept and cleared,
  the name following the data and being carried by a measurement). Eight tests for enrollment and revocation (single-use as before, reusable counts and stops at revocation or expiry, 30 days,
  community tier, a revoked node gone from both lists and refused, a legacy revoked row hidden and swept, no rotation endpoint).
  Three tests for the addresses endpoint (an owner, a stranger and a shared link, with the lookup API closed to administrators
  only; no database; a private report). Estimator tests for what used to be refusals (1 and 2 nodes, a node 140 ms away, nodes 3,600 km apart), for the figure's shape
  (never rising with the closest node's round trip, 1 to 99, headline equals the 500 km figure), and on the real data: no unicast
  fleet is flagged, every measurement gets an answer and a far one is `low`, the stated accuracy matches the held-out half, and 95%
  or more of the anycast fleets are flagged. API tests for `accuracy` and `conflicting`. 472 pass on SQLite, ruff and mypy clean.

## v0.9.0 (2026-11-10)

Released and verified: the files match `SHA256SUMS` and carry attestations (`gh attestation verify`), a PostgreSQL database that
the released v0.8.1 image had migrated (with a measurement in it) upgrades to `0012` with the released v0.9.0 image and keeps the
row, and the released image with the simulated fleet measured `example.org` at two addresses by default (estimate grouped 12 / 4),
and at one address, Tokyo's, when Tokyo was asked to resolve it, with the combination that makes no sense refused.

Who resolves a hostname changes: **each node resolves it itself by default**, and a measurement can ask **one chosen node**
to resolve it for everybody. The database migrates to `0012` (`measurements.resolved_by`).

### What changed
- `dns_mode` defaults to `regional` (it was `fixed`): every node is sent the name and probes the address that is near it. Anyone
  who relied on the old default sends `"dns_mode": "fixed"`. An IP address is still the same for every node, and a watch made
  from now on defaults to `regional` too (watches that exist keep the mode they were saved with).
- New `resolve_node_id` (with `dns_mode` `fixed`, a hostname target): that node asks its own resolvers for the name, and every node
  measures the address it found. The question is the DNS check the nodes already run (`system` resolver, A; AAAA when IPv6 is
  asked for, or when there is no A record), staged as a measurement that nobody sees and removed once it is answered, so nodes
  need no new version and the backend instances share nothing new. `fixed` without a node still means the backend resolves it.
- What the node found goes through the same policy as an address the backend resolved: private addresses and the blocklist are
  refused (a resolver answer the node left out as private, which it only counts, refuses the measurement), a name the node cannot
  resolve is `dns_failure` with the node's name, a node that does not answer in about ten seconds likewise, and a node that is
  offline, waiting for approval or without DNS checks is `node_unavailable`. A volunteer's node can be chosen (like `node_ids`)
  and counts against its per-minute limit.
- `resolve_node_id` with `dns_mode` `regional`, or on a `dns` check, is `invalid_option`; on an IP address it is ignored.
- The measurement says who resolved it: `resolved_by` (the node as it was at the time), in `GET /measurements/{id}` and as a
  line in the text output. `GET /api/v1/text` takes `dns_mode` and `resolve_node_id` too (its default is `regional` as well).
- The **location estimate** works for names the nodes resolved themselves: nodes are grouped by the address they measured, the
  estimate is for the address most of them measured, `groups` lists the rest with their node counts, and `?ip=` estimates another
  one. Before, such a measurement had no estimate.

### Tests
- `tests/test_resolve_node.py` (16): the default, the chosen node asked its own resolvers about the name as typed, every node
  measures what it found, a node that is not among the measuring ones, IPv6 and the IPv4-first fallback, private and blocklisted
  answers, NXDOMAIN and SERVFAIL, no records, a node that never answers (cancelled, and nothing left in the database), offline or
  not able, awaiting approval, combinations that make no sense, the text endpoint. The tests that relied on the old default now say
  `fixed`. 452 pass on SQLite and 464 on PostgreSQL, with ruff and mypy clean.

## v0.8.1 (2026-11-10)

Fix to the node auto-fill of v0.8.0, found when the released files were tried on a fresh machine: a node that reported
DE / Falkenstein, whose address the database placed in California, ended up in Germany with a Californian region and position.
The location estimate treats a node's position as a fact (a round trip bounds the distance), and a hosting address is often
placed at its provider's headquarters, so that is not a cosmetic problem. No database change (still `0011`).

### What changed
- The data's **place** (country, region, city, position) is used only where it agrees with the place somebody gave. A conflict
  is: a typed or reported country that is not the data's; a typed position more than 300 km from the data's; a typed or
  reported city that the gazetteer places more than 300 km from it (a city it does not know says nothing). On a conflict no place
  is taken from the data and any place it supplied earlier is taken back; the provider and the AS number, which belong to the
  address, are still filled in. Fixing or clearing what disagrees lets the data fill the place again.
- A record with **no city name** (the centre of a country or region) is no position for a node, and is not used as one.
- Editing a node now reconciles at once: typing a country that disagrees with the automatic place takes the automatic place back
  without waiting for the node to reconnect.

### Tests
- Five new tests (another country, a far and a near city, a typed position far and near, typing a disagreeing country and clearing
  it, a record with no city); each rule fails its test when switched off. 433 pass on SQLite and 445 on PostgreSQL, with ruff and mypy
  clean.

## v0.8.0 (2026-11-10)

IP data: MaxMind GeoLite2 turned on from the admin page, a lookup API, and nodes filled in from their address. The database
migrates to `0011` (`nodes.auto_fields`).

### MaxMind, from the admin page
- `GET/PUT /api/v1/admin/ip-data` and `POST /api/v1/admin/ip-data/update`: the **account ID and license key** (MaxMind's
  current download service needs both, as Basic credentials; the old license-key-only URL no longer accepts new keys), which
  editions (GeoLite2 City, Country, ASN), automatic updates and how often (6 to 168 hours, default 24), and "update now". The
  key is write-only: it is accepted, stored and used, never returned, never in an audit event, an error or a log line. Or give
  it in the environment (`IPPING_MAXMIND_ACCOUNT_ID`, `IPPING_MAXMIND_LICENSE_KEY`), which then wins.
- `ipdata/maxmind.py` downloads `.../geoip/databases/<edition>/download?suffix=tar.gz` with Basic auth, follows MaxMind's
  redirect **without** the credentials, checks the archive against MaxMind's published SHA-256 (`tar.gz.sha256`), takes only
  `<edition>.mmdb` out of it (size-limited), opens it as a database of that edition that answers for 8.8.8.8, and replaces the old
  file atomically. A 401/403, 404, 429 and a broken connection each become a plain message.
- `ipdata/updater.py` runs in every process: a check is due `update_hours` after the last attempt (1 hour after a failure, 6
  after a refused key or the daily limit), asks MaxMind with a free HEAD request whether anything newer exists (GeoLite2 allows
  30 downloads a day), takes a file lock so one process per host downloads, and keeps `maxmind-status.json` in the GeoIP
  directory so every process (and the page) shows the same result. Giving a key starts a download at once; a new database is in
  use without a restart.
- The GeoIP loader now finds files whatever the case of their name (`GeoLite2-City.mmdb` did not match `*city*`), prefers
  MaxMind's own files to any other, and knows a Country database.

### The lookup API
- `GET /api/v1/ip/{address}` and `GET /api/v1/ip`: network, country, region, city, postal code, position and accuracy, time
  zone, AS number and organisation, with the source, when the database was built and the attribution. A private or reserved
  address says `public: false`; an unknown one `found: false`; text that is not an address is a 422; no database is a 503 that
  says how to get one.
- Settings on the admin page: on or off, who may use it (`api_key` by default: an API key or an administrator; `public`;
  `admin`), and lookups a minute per client address (60; administrators are not limited).

### Nodes filled in from their address
- A node's country, region, city, latitude and longitude, provider and AS number are filled from the data for the address it
  connects from, when it enrolls and when it connects, and when a database is replaced. **A field an administrator typed, or the
  node reported when it enrolled, is never overwritten**; a field that was filled automatically follows the data; editing makes it
  yours and clearing hands it back (`nodes.auto_fields`, shown on admin nodes as `auto_fields`). `location_verified` is never set
  by it. `POST /api/v1/admin/nodes/{id}/autofill` fills one now; the whole thing can be turned off, and turning it on fills what
  is empty.
- 41 tests more: the download against a pretend MaxMind (checksum, redirect, bad archives, every status, no key in any message),
  the updater (due times, no repeat downloads, one process per directory, a status file that survives), the page, the lookup and
  its access modes and limit, and each rule of the auto-fill. 429 on SQLite and 441 on PostgreSQL.
- Not tested against MaxMind itself (that needs an account): the endpoint, the `.sha256` file and the HEAD behaviour follow
  MaxMind's published documentation.

## v0.7.0 (2026-11-10)

DNS over HTTPS to Quad9, and geofeeds. No database change (head `0010`). Nodes need v0.7.0 for the first; the second is
the backend's alone.

### DNS over HTTPS to Quad9
- `transport` `doh` now accepts `resolver` `quad9` (`dns.quad9.net`). Quad9 answers only over HTTP/2, which nodes speak from
  v0.7.0, so a measurement that asks for it goes only to nodes of that version or newer: a v0.6.x node would get "505 HTTP
  Version Not Supported" and report a protocol error. `protocol.HTTP2_ONLY_DOH` names the resolvers and the version; when no
  online node qualifies the error says so ("... a dns measurement over DoH to quad9 (it needs HTTP/2, which nodes speak from
  v0.7.0) yet"), and Cloudflare and Google stay available to every v0.6.0 node.
- 3 tests (which versions speak it, a mixed fleet, every node too old); the refusal that was tested for Quad9 is now
  tested for a node's own resolver, which is still refused.

### Geofeeds (RFC 8805), opt-in
- `ipping-backend geofeed-update` fetches the geofeeds listed in `IPPING_GEOFEED_URLS` (or `--url`) into the GeoIP directory.
  A result whose address a feed covers gains `geofeed`: the feed's host, the prefix, what the feed says (`claim`) and, when the
  gazetteer knows the city, a verdict the way the GeoIP database's is (`ruled_out` by a node with a verified position,
  `consistent`, `not_checked`). It never changes the estimate. The admin status says how many prefixes are loaded.
- **Bounded on purpose**: only the listed feeds, only when the command runs (nothing leaves the server because of a
  measurement); https only with redirects to https only, a host whose addresses are all public, 32 MB and 30 s a feed, 500,000
  lines a feed and 1,000,000 in all; a line that is not a valid public prefix (not broader than a /8, or a /16 for IPv6), has a
  bad country or says nothing is skipped and counted; a feed that fails keeps its lines; the file is replaced atomically and
  read again when it changes.
- Tried on the real feeds of Cloudflare, AS13030 and Telstra Global: 151,633, 4,158 and 1,020 prefixes (1.1 MB stored); an
  http, a localhost and a name that does not resolve are refused with their reasons; an address in a Cloudflare line is placed
  in Singapore or Tokyo with coordinates, an AS13030 line names a Swiss town the gazetteer lacks (shown as the claim), Telstra's
  lines are country only.
- **Discovery from the registries was weighed again with real numbers and left out**: the RIR databases that carry the
  `geofeed:` attribute are about 340 MB, and RIPE's IPv6 file has 599,826 such lines for only 2,141 different feeds (one broker
  accounts for 585,735), each a URL its owner chose. Listing the feeds you care about is the supported way.
- 20 tests (the parser and what it skips, longest-prefix lookup, storage, a feed that fails, limits, https-only and redirects,
  the result's verdicts, the admin count, the command); 388 on SQLite.
- **Community nodes: signed results and reputation were reconsidered and still not built**, with the reasoning written down in
  the README: signing proves which node, not that it told the truth (the credential already says which); a reputation has no
  ground truth to score against but the same nodes; a known-answer check catches only a node that does no work and is passed by
  a node that recognises it.

## v0.6.1 (2026-11-09)

A fix found by installing the released v0.6.0 over a real v0.5.1 stack. No database change (head `0010`).

### fix: a node that cannot do DNS over HTTPS or QUIC is not asked for it
- A node older than v0.6.0 does not know the `transport` option, ignores it, and does the plain thing: a DNS question
  over UDP for `doh`, a TCP handshake for `quic`. Both came back `ok`, with nothing to say they were not what was asked
  for (the cards show the transport the node reports, so they looked like an ordinary check). During a rolling update that
  is every node not yet updated.
- A measurement with `transport` `doh` or `quic` now goes only to nodes of v0.6.0 or newer (the version the node
  announces; one that cannot be read counts as too old, since a wrong "no" leaves a node out and a wrong "yes" gives the
  wrong answer). When no online node qualifies, the error says what is missing ("None of the 2 online nodes can run a dns
  measurement over DoH yet. Nodes need to be updated to a newer version."); when others qualify, a request that names an old
  node by id gets `node_unavailable`. Plain DNS and TCP certificate checks still go to every node.
- 4 tests (which versions count, a mixed fleet, every node too old for DoH, a node without QUIC) and the test node
  announces v0.6.0 unless told otherwise. 372 on PostgreSQL, 360 on SQLite.

## v0.6.0 (2026-11-09)

Community nodes (a first slice), DNS over HTTPS, and QUIC for the certificate check. The database migrates to `0010` (a tier on
nodes and enrollment tokens, `official` for everything that exists, and a stored location audit). Nodes need v0.6.0 for DNS over
HTTPS and QUIC; an older node keeps working for everything else.

### QUIC as a transport of the certificate check
- `transport` now also names how a `tls` check reaches the server: `tcp` (the default) or `quic`, a QUIC handshake over
  UDP offering `h3` (the transport of HTTP/3). The same detail, cards and watch rules apply, and the sample adds
  `transport: "quic"` and `alpn` (the protocol agreed). Each kind refuses the other's values (`udp`/`doh` for a certificate
  check, `tcp`/`quic` for DNS) with a message that says which are allowed. The plain-text report ends `(QUIC, h3)`.
- 3 tests (the plan and the refusals, the detail's limits, a measurement through a node) and the example frames `task_tls_quic`
  and `sample_tls_quic` (also in the node repository). 347 on SQLite and 359 on PostgreSQL.
- **Geofeeds (RFC 8805) were looked at and not built.** Asking the real registries: of six well-known networks only Cloudflare
  publishes a geofeed, and only as free text in an ARIN remark ("Geofeed: https://..."), not as the RFC 9632 `geofeed` link;
  Google, GitHub, Amazon, APNIC and RIPE publish none. Building it would send every target address to a third-party RDAP
  service and fetch a URL its owner chose, for evidence that most targets do not have. The DB-IP cross-check stays.

### Community nodes (first slice)
- Migration `0010`: `nodes.tier` and `enrollment_tokens.tier` (`official` by default, so everything existing is unchanged),
  and `nodes.location_audit`. A token's tier becomes the tier of the node that enrolls with it; an administrator can
  change a node's tier. Public nodes, admin nodes and every task's frozen node info carry `tier`.
- "All nodes" means the official ones; `include_community` (a request field, `/api/v1/text` parameter and, in the
  interface, a box) adds the online community nodes, and choosing a node by id always works. Watches use official nodes.
- Each community node is asked for at most `community_tasks_per_minute` tasks a minute (a runtime setting, default 20):
  "all nodes" quietly leaves out one that is at its limit, a request that names it gets `node_busy` (429).
- A community node whose location is not verified is not evidence for a location estimate, so a volunteer cannot move one
  by reporting a lie. (The estimate already refused to let an unverified node rule a place out.)
- `POST /admin/nodes/{id}/audit-location`: asks at least three verified official nodes to ping the address the node
  connects from and checks the declared place against their round trips with the physics the estimate uses; the result
  is `consistent` (not contradicted, with a verified node near enough to matter), `inconsistent` (names the node that
  ruled it out) or `inconclusive` (too few or too far, or an address that is not public), stored on the node. It verifies
  nothing by itself.
- What this is not (also in the README): it does not make a volunteer's results trustworthy, verify a place, protect
  an operator from the service's own targets, or add signed results or reputation.
- 11 tests: the judgement (consistent, inconsistent naming the witness, too few, too far), the token's tier and changing
  it, default exclusion and opt-in and choosing by name, the rate limit (both behaviours and official nodes unlimited),
  a lying volunteer not moving an estimate, and the audit through the API (consistent, inconsistent, a private address,
  no declared place, administrators only). 340 on SQLite, 352 on PostgreSQL.

### DNS over HTTPS
- A DNS check takes `transport` (`udp`, the default, or `doh`): the node asks the public resolver over HTTPS (RFC 8484,
  `GET /dns-query?dns=...` on port 443, the certificate checked for the resolver's name) instead of UDP port 53. It is
  asked of **cloudflare** (`cloudflare-dns.com`) and **google** (`dns.google`); the plan gives the node the address, the
  name and the path (`resolver_ip`, `doh_host`, `doh_path`), so a node never asks a server the backend did not name.
- **quad9 is refused, with the reason**, because testing against the real resolvers showed that Quad9 answers DNS over
  HTTPS only over HTTP/2 ("505 HTTP Version Not Supported ... requires HTTP/2", which RFC 8484 allows) and the nodes
  speak HTTP/1.1. The nodes' own resolvers (`system`) cannot be asked over HTTPS either. Both say so in the error.
- The sample's detail gains `transport: "doh"` (absent for plain DNS); the plain-text report marks it `(DoH)`;
  `MeasurementOptions.transport`; the text endpoint and watches take `transport` too. Contracts regenerated; example frames
  `task_dns_doh` and `sample_dns_doh` (also in the node repository).
- 12 tests: the plan for each resolver, plain DNS unchanged, the refusals and their reasons, the detail and option limits,
  a measurement through a node, the text endpoint. 329 on SQLite and 341 on PostgreSQL.
- Needs node v0.6.0 to ask over HTTPS; an older node would ignore `transport` and ask over UDP, so the
  result would not say `doh` (it is shown by what the node reports, not by what was asked).

## v0.5.1 (2026-11-09)

One fix, found by installing the released v0.5.0 on a fresh Debian 13 and reproduced on the released image by freezing
the container for 25 seconds (the node went from online to offline and its row vanished); the fix is below. Nothing else
changed, and no migration.

### fix: a backend that was paused took itself for dead and dropped its nodes
Found by installing the released v0.5.0 on a fresh Debian 13 in a container whose machine kept going to sleep: the
node was approved and connected, and the site listed it as offline for over an hour.
- The reaper deletes the connection rows of every instance whose last heartbeat is older than 10 seconds, and
  that included the instance it was running in. After any stall longer than that (a suspended machine or VM, a long
  pause, or PostgreSQL being away for a while: nothing could be heartbeated meanwhile) the reaper woke up, found its
  own heartbeat stale and deleted the rows of every node it was holding, and interrupted the measurements it owned.
  The sockets stayed open, so the nodes had no reason to reconnect and stayed "offline" and unused until the service
  or the node was restarted. It happened on any single instance and on every release since the cluster work.
- The reaper now always counts its own process as alive. And every heartbeat (3 seconds) puts back the rows of
  connections the process really holds if they are gone, but only where no row exists for the node, so a node that
  reconnected to another instance is never taken back. A restore is logged as a warning.
- 4 tests (the stalled process keeps its connection; lost rows come back while the socket is open; a node that
  moved to another instance is left alone; a node that disconnected is not put back). The first fails on the old
  code with the symptom seen on Debian. Suites: 313 on SQLite, 325 on PostgreSQL (the two-instance tests included).

## v0.5.0 (2026-11-09)

Traceroute in rounds like `mtr`, the networks and places of a route's routers, and certificate checks. The database
migrates to `0009` (`tls` is added to the saved kind list of a deployment that never narrowed it). Nodes need v0.5.0
for rounds and certificate checks; an older node keeps working for everything else.

### 2026-11-08: certificate checks
- A new kind, `tls`: ask every node for a TLS handshake with the target (a name, presented to the server and
  checked against the certificate, or an address) on a port from the allowed list, 443 unless another is given
  (`host:port`, a URL or `port`). No request is sent. The sample's `tls` detail has the version, cipher, subject,
  issuer, validity, whether the node trusts the certificate and why not, the names it is valid for (up to 24) and
  how many certificates the server sent. A certificate that is not trusted is an `error` with
  `tls_certificate_invalid` that keeps all of it, so a result can say what is wrong with it.
- **Watches** judge it: good when trusted and with at least 14 days left; a certificate that is valid but nearly
  out of time makes the run `degraded` ("the certificate expires in 9 days") rather than `down`, so the alert
  comes before the outage; an untrusted or missing one is `down`. The run is judged with a clock that tests can set.
- Migration `0009` adds `tls` to the saved kind list of a deployment that never narrowed it (as `0006` did for
  `dns`); a deliberate restriction is kept. A new deployment has it from the default.
- Probe cost 2 (a connection and a handshake, like an HTTP check), deadline from the 8 s handshake timeout, the
  text report prints trust, version and expiry per node. Contracts regenerated; example frames `task_tls`,
  `sample_tls` (also in the node repository) and `hello_all_kinds` brought up to date.
- Tests: planning and ports, the detail's limits and cleaning, the watch rules, the migration, and a measurement
  through a node (trusted, not trusted, bare address, port refused, a node that cannot do it is not asked). The
  full suite also passes on PostgreSQL (317 tests).

### fix: a control character at the end of a text field was kept
- The cleaner for text a node sends (a server name, a certificate subject and the like) used a regular
  expression ending in `$`, which also matches just before a final newline, so `"nginx\n"` counted as printable
  and was stored with its newline. It now matches the whole string. Found while testing the certificate detail;
  the existing test only had the control character in the middle.

### 2026-11-07: the network and place of each router on a path
- `GET /api/v1/measurements/{id}/path` returns, for every public router a traceroute found, its network (ASN
  and organisation) and the place the GeoIP database gives, keyed by address, plus which databases are loaded
  and their attribution. It is empty when no database is set up and for any kind but traceroute. Same access
  as the measurement itself (owner, API key, administrator or a shared link).
- Done on every request and not stored, like the GeoIP check of the location estimate: the databases are
  replaced every month. Only addresses the result already shows are looked up, and only public ones.
- A router's place is a hint, not a fact (routers are often registered at their owner's headquarters); the web
  interface checks it against the round-trip time before drawing it.
- 5 tests with generated databases: no database, both databases, a network without a city database, a ping,
  and access (a stranger gets 404 until the owner shares). Contracts regenerated.

### 2026-11-07: traceroute in rounds, like mtr
- A traceroute now takes `count` (1 to 10 rounds, default 1; the deployment's `max_count` still caps it). The
  node traces once and then probes every hop it found again, a second apart, so a hop's loss and spread can
  be seen. A hop is still one sample; its `detail` gains `sent` (probes sent to that hop) and holds every
  reply in `rtts_us` (up to 32). A one-round trace is exactly as before and `sent` stays null.
- `MeasurementOptions.count` is the number of rounds for a traceroute. The deadline grows by about two
  seconds a round, and the probe budget counts a third of the hop limit for every extra round (one packet per
  hop against the three of the first round). The plain-text report prints loss and best/avg/worst per hop.
- Tests: planning, cost and deadline, the hop model's limits (more answers than probes, no probes), the
  fixtures `task_traceroute_rounds` and `sample_hop_rounds` (also in the node repository), and a measurement
  through a node with four rounds. Contracts regenerated.
- Needs node v0.5.0 or newer to repeat; an older node ignores `count` for a traceroute and traces once.

## v0.4.1 (2026-11-07)

Two defects found by installing the released v0.4.0 on a fresh Debian 13 and in a Docker stack, and using it.

### fix: GeoIP databases downloaded after the server started were not noticed
- The directory was looked at only when the server started, so `ipping-backend geoip-update` into the running
  stack's volume (what the Docker installer does) downloaded the databases and the admin page still said none
  were loaded until a restart; only a file that was *replaced* was picked up. The directory is now scanned again
  every 10 seconds (when a lookup is made), so a first download and each month's newer file are found without a
  restart, as the README said. Tested: files that appear after construction, a newer file replacing an older, and
  that the scan is not repeated on every lookup.

### fix: an alert that did not arrive was never tried again
- If the webhook or ntfy server was down at the moment an outage began, the delivery failed, the failure was
  recorded on the watch, and the watch then counted as "alerted": later bad runs sent nothing, so the administrator
  could stay unaware of an outage that was still going on. A failed alert is now sent again on each following bad
  run until it is delivered, once, and not again for the same outage. (A failed "it is back" message is still not
  retried.) Tested with a webhook that is down at the first run and up at the second. Seen on the real stack: a
  watch created before its listener was running.

## v0.4.0 (2026-11-07)

### feat: DNS checks, a GeoIP cross-check, watches, API keys
Five things, newest first below: a place for the GeoIP databases in the image, API keys with their own quotas and
a plain-text endpoint, **watches** (scheduled checks with quiet, address-safe alerts, safe with several
instances), an optional **GeoIP / ASN database** that is checked against the measurements, and the **`dns`
kind** (needs node v0.4.0). Database migrations `0006` to `0008`; `httpx` and `maxminddb` are now dependencies.


### 2026-11-07: the image has a place for the GeoIP databases
- The container image creates `/data/geoip` (owned by the service user) and sets `IPPING_GEOIP_DIR` to it, so a
  named volume mounted there is writable by the read-only container and `ipping-backend geoip-update` can fill it
  (the application's compose file and installer now do exactly this). Empty, nothing changes.

### 2026-11-07: API keys, quotas and the text endpoint
- **API keys** for scripts: created by an administrator (a name and quotas), shown once as `ipk_...`, stored as a
  SHA-256. With `Authorization: Bearer` (or `X-API-Key`) a script starts, lists, reads and cancels measurements
  with no browser session, cookie or CSRF token. A key may start `per_minute` (30) and `per_day` (1,000)
  measurements, counted from the measurements it actually made, and run as many at once as a visitor; the
  per-address and per-session limits browsers get do not apply to it. Another key, or no key, sees a 404 for its
  measurements; administrators can open them. A wrong or revoked key is a 401 instead of being ignored.
- **`GET /api/v1/text`** starts a measurement and returns the result as plain text (the report `ping` or
  `traceroute` would print), waiting up to `wait` seconds (20 by default). It needs a key and never a session,
  because a GET with a side effect must not be something a web page can make a browser do. An invalid query is a
  422, not a 500 (found by the tests: a validation error raised by hand escaped the error handler).
- Migration `0008` (`api_keys`, `measurements.owner_api_key_id`). 15 new tests: only a hash is stored and the key
  never appears in the audit trail, quotas per key (minute, day, concurrency), keys cannot see each other's
  measurements, revoked and malformed keys, last use, the text endpoint with a fake node answering while the
  request waits, and the report for each kind.

### 2026-11-07: watches: scheduled checks and alerts
- A **watch** runs a measurement on a schedule (every 60 s at the least) and alerts when the target stops
  answering. A run is `ok`, `degraded` (fewer than `min_ok_pct` of the nodes got a good answer) or `down`, judged
  by what the kind means by an answer: a ping reply, an opened connection, an HTTP status below 500, a DNS
  answer with `NOERROR` and a record, a route that reached its destination. A check that cannot run (no node
  online, a restart) is an `error` run and never an alert.
- **Quiet alerts**: once after `alert_after` bad runs in a row, nothing while it stays bad, once when it is
  back. To a JSON webhook or an ntfy topic. A failed delivery is recorded on the watch. The address is treated
  like a target: resolved here, every address public unless `allow_private_webhooks` is on, no credentials in
  it, the connection goes to the checked address (with the name for Host and TLS), no redirects, and it never
  appears in the audit trail.
- **Safe with several instances**: every instance runs the scheduler and a due watch is *claimed* with a
  conditional update, so it runs once per interval; tested with two schedulers ticking at the same moment.
- Migration `0007` (`watches`, `watch_runs`, `measurements.watch_id`); `/api/v1/admin/watches` (list, create,
  change, delete, run now, test alert, runs); runtime settings `max_watches`, `min_watch_interval_s`,
  `watch_history_days`, `allow_private_webhooks`; the retention sweep prunes old runs. `httpx` is now a
  dependency. 39 new tests (judging per kind, the alert rules as a table, the formats, address safety including
  that the connection is pinned to the checked address, the API, a full run with a fake node, the retention).
  (An unrelated existing test, `test_http_measurement_asks_for_the_name...`, failed once in the first full run after
  this change and then passed in 8 more full runs and 15 repeats of its module: a timing flake, not reproduced.)

### 2026-11-07: a GeoIP / ASN database as a second opinion
- Optional. With `IPPING_GEOIP_DIR` (or explicit `IPPING_GEOIP_CITY_DB` / `IPPING_GEOIP_ASN_DB`) a result's
  location gains `asn` (the network of the target) and `geoip`: the place a MaxMind-format database names,
  its distance from the estimate, and a verdict. `ruled_out` names the node that makes the database's place
  impossible (a node with a verified position that heard from the address in fewer milliseconds than light
  needs to get there and back), so a database that puts an anycast address in one city is shown to be wrong
  instead of believed. `consistent` and `not_checked` say what they say. The estimate is unchanged: it stays
  the calibrated latency model, and the database is never an input to it.
- `ipping-backend geoip-update` downloads the free DB-IP Lite city and ASN databases (CC BY 4.0; the result
  carries the attribution), checks that each opens, is of the right type and answers for a well-known
  address, and swaps it in atomically, so a server reading the directory never sees a half-written file.
  Tried against the real files (126 MB city, 9 MB ASN): 1.1.1.1 is "Sydney, AS13335 Cloudflare", which is
  exactly the case the cross-check exists for. The server reopens a database when its file changes: no restart
  after the monthly update. Nothing is fetched while serving a request.
- Admin status says whether the databases are loaded. 17 new tests (reading, replacing, newest file in a
  directory, ruling out, the API with verified and unverified nodes, the updater with a fake download
  including a corrupt one and the wrong kind). Geofeeds (RFC 8805) are not used.

### 2026-11-07: DNS checks
- A new kind, `dns`: every node asks a resolver about a name and the answers are compared. Options
  `record_type` (`A` by default, `AAAA`, `CNAME`, `MX`, `NS`, `TXT`, `SOA`) and `resolver` (`system`, the
  node's own resolvers and the default, or `cloudflare`, `google`, `quad9`: a public resolver asked
  from every node, mapped to its address here so no other server can be named).
- The backend never resolves the name, so a name that does not exist (the interesting case) can be
  measured; an IP address, a single label and internal names (`.lan`, `.corp`, `.home.arpa` ...) are
  refused, and the deployment blocklist applies to the name.
- The sample `detail` is a new `DnsDetail`: resolver, record type, response code, authoritative, via TCP,
  truncated, up to 32 records and a count of redacted answers. Text is stripped of control characters.
  From a node's own resolver, answers that are private addresses are left out here too (a node already
  does this; the backend does not rely on it), and a resolver that is not a public address is shown as
  `unknown`. The size limit of a sample's detail is 8 KiB (was 4 KiB) so a full answer fits.
- Migration `0006` adds `dns` to `enabled_kinds` for deployments that saved the list with all four
  older kinds on; one whose administrator turned a kind off keeps its narrower choice.
- Contracts regenerated, with example frames (`task_dns`, `sample_dns`) that the node parses too. 217 tests
  pass on PostgreSQL (205 on SQLite), including the privacy filtering, planning rules and the migration.

### 2026-11-07: README brought up to date
- The README now covers all four kinds in the intro and diagram, the additive protocol and the rule
  that everything from a node is untrusted, the complete configuration and runtime settings, an
  overview of the API (including the `POST /measurements` body), what a sample is for each kind and how
  the probe budget counts, the crash test including the retried tasks, the known limits (public IPv6
  untested, no GeoIP), a release table with the migration head of each release and the upgrade that
  was tested, and a roadmap that matches v0.3.0. Test counts are current (175 on SQLite, 187 on
  PostgreSQL).

## v0.3.0 (2026-11-06)

TCP, HTTP and traceroute measurements; tasks sent again when a backend instance dies; a location model
checked and fitted on real measurements. Database migrations 0004 and 0005 (run `ipping-backend migrate`).
Nodes of any earlier version keep working for ping; they need v0.3.0 for the new kinds.

### 2026-11-06: the location model, checked against real measurements
- Until now the estimator had only been checked on synthetic paths. `scripts/collect_atlas.py`
  gathers public RIPE Atlas data: the round trip from ~1,000 anchors (known positions) to each of 56
  anchors in 40 countries, and to seven anycast root servers; the snapshot is committed
  (`calibration/`, 340 KiB). `scripts/evaluate_location.py` draws random fleets of 4 to 32 anchors, asks
  the estimator where the target is and compares with the truth. Targets are split in two: constants were
  tuned on one half, and the other half is only used to report.
- Findings, model 3 on the held-out half: median error 658 km over all answers; `strong` within 100 km
  72% of the time and within 300 km 99%; error grows steadily with the closest node's round trip (median 43 km
  below 5 ms, 3,300 km above 80 ms), so answers from a far-away fleet were close to useless.
- Changes (model 4): the fixed delay of a path is 8 ms, not 1 ms (the one change that mattered: it
  fits real paths, and cut the error in every band); no answer when the closest node is over 40 ms
  (was 100 ms); `moderate` needs a node within 10 ms (was 40); two nodes that contradict each other at
  110 km per round-trip ms (was the speed of light in vacuum, 150) mean "inconclusive". The rest was
  tried (tolerance, inflation range and prior, evidence size) and left alone.
- Result on the held-out half: median error of answered runs 293 km (p90 1,420 km); `strong` 100%
  within 300 km (p90 190 km), `moderate` 99% within 500 km, `low` 75% within 1,000 km; 36% of random
  fleets get no answer. Anycast: 95% of 1,120 fleets measuring root servers are "inconclusive" (558 of
  560 with 16+ nodes); 0 of 6,720 real unicast fleets were. Full tables and the caveats (data-centre
  targets, an Europe/North-America-heavy pool, city-level coordinates) are in the README.
- `tests/test_real_measurements.py` guards these numbers with margin. Estimates stored with model 3 are
  recomputed on demand (the model version is part of the cache key).

### 2026-11-06: a task whose backend instance died is sent again
- Before, a task claimed by an instance that then died was always `interrupted`: delivery was at most
  once. Now, if the node had not produced anything yet, the task goes back to `pending` as a **new
  attempt** (new `attempt_id`, so a late frame from the old one is ignored; `attempts` is exposed on
  the task) and is sent to its node as soon as it reconnects, to whichever instance. If the node
  already produced samples, starting over would throw them away, so the task is `interrupted` and the
  partial results are kept as before. `task_retries` (default 1, 0 turns it off) limits it.
- Only backend failures are retried. A node that drops out stays `node_disconnected`: retrying would
  pass a flaky node off as a reliable one. A task that waits for a node that never returns still fails
  after the grace period (`IPPING_PENDING_GRACE_S`, default 5 s), counted from when it was requeued.
- After a restart the new process does the same for what the old one left (it used to close
  everything as `interrupted`).
- Real run: 3 worker processes and 6 real nodes; the worker holding 3 nodes was SIGKILLed while the
  measurement (TCP to a port that drops packets, so no sample yet) was running. The measurement
  settled in 13.6 s as `completed`, all 6 tasks with their 2 samples, the 3 that were on the dead
  worker with `attempts` 2. (A ping that has already produced samples is interrupted, as designed.)
- Tests: unstarted task retried and old frames ignored, partial results kept and interrupted, retries
  off, retry limit, node that never returns (PostgreSQL), restart behaviour (SQLite). Migration 0005.

### 2026-11-06: TCP, HTTP and traceroute measurements (backend side)
- `kind` on `POST /api/v1/measurements` now accepts `tcp`, `http` and `traceroute` besides `ping`.
  Protocol v1 stays v1: the changes are additive (`TaskTarget.port`/`server_name`, `TaskOptions`
  replacing `PingOptions`, `SampleFrame.detail`), a node only gets kinds it advertised, and null
  fields are no longer sent, so a v0.2 node sees exactly the ping frames it did before.
- **tcp**: time to open a connection to a port from a short, administrator-editable allowlist
  (`allowed_ports`), because a service that connects to any port is a port scanner. A refused
  connection is an `error` sample that keeps its time (the host answered) and still is not counted
  as a successful connection.
- **http**: one request with DNS/connect/TLS/first-byte timings, status, the server header, redirects
  and the TLS certificate (subject, issuer, validity, whether it verified). Only the path of a pasted
  URL is kept, never its query string or credentials. Redirects are not followed unless asked for, and
  at most `max_redirects` (default 5, 0 turns following off); every hop is checked on the node, but the
  administrator's denylist applies to the first address only.
- **traceroute**: one sample per hop. A hop that is not a public address (the node operator's router,
  carrier-grade NAT) is reported as "a private hop" with no address, so a result never discloses
  someone's LAN. A route is judged by whether the destination was reached, not by silent routers.
- Everything a node sends beyond a round trip (`detail`) is parsed against a model for the task's
  kind, size-limited, stripped of control characters, and dropped if it does not fit. The sample
  itself is kept.
- Location estimates use ping and TCP round trips only. New admin settings: `enabled_kinds`,
  `allowed_ports`, `max_redirects`, `traceroute_max_hops`. Migration 0004 (`samples.detail`).
- The node lists (`GET /api/v1/nodes`, admin nodes) say which kinds each node can run, so a UI can tell "3 online, 2 can run HTTP".
- 26 new tests (planning, validation, each kind through a fake node, version gating, admin settings,
  hostile details); the whole suite also passes on PostgreSQL 16.

### 2026-11-05: more than one backend process
- Several backend processes (or hosts) can serve one deployment, with PostgreSQL as the only shared
  piece: instance heartbeats and node-connection presence in tables, the instance holding a node's
  connection claims and sends that node's tasks, a reaper in every instance settles deadlines, dead
  instances and abandoned tasks, and `LISTEN/NOTIFY` wakes the others (every consumer also polls, so a
  lost notification costs latency, never a result). Live streams tail the event table.
- Rate limits are counted in the database and first-run setup is race-proof across instances, with a
  shared setup token. `serve --workers N` / `IPPING_WORKERS`. Migration 0003.
- Replaces the startup "interrupt everything" step and the per-measurement timers, which assumed one
  process. Restart recovery is now immediate for a clean stop and takes up to the dead-instance timeout
  after a crash.
- Found by the tests and fixed: a node's old socket stayed registered while being closed by a remote
  instance, so a new task could be claimed for it and fail.
- Real run: 3 worker processes, 6 real nodes; the worker holding 4 nodes was killed mid-measurement,
  the measurement settled in 7.4 s as `interrupted` with exactly those 4 tasks (the other 2 completed)
  and every node reconnected. 8 two-instance tests in `tests/test_cluster.py` (PostgreSQL only).

## v0.2.0 (2026-11-05)

### 2026-11-04: estimated network location
- `GET /api/v1/measurements/{id}/location`: where the target appears to be, from the nodes' smallest
  round-trip times and positions. Speed-of-light limits exclude impossible places; remaining cities are
  ranked with a fitted path-length factor; contradictory evidence is reported as inconclusive and thin
  evidence as insufficient, with the evidence, alternatives, warnings and limitations attached.
- Stored per finished measurement (`location_estimates`, migration 0002) and redone only when the
  model version changes. Node snapshots now record whether an administrator verified the coordinates.
- City list from GeoNames (CC BY 4.0, `NOTICE`), rebuilt by `scripts/build_gazetteer.py`.
- Checked on synthetic paths: 399 randomized trials, every `strong` within 218 km, every `moderate`
  within 272 km, `low` median 352 km (worst 8,232 km); consistent data is never called contradictory.
  Real-world accuracy has not been measured.

### 2026-11-04: signed releases
- Release files get signed build-provenance attestations (`gh attestation verify`); takes effect with the next release.

## v0.1.0 (2026-11-04)

First release. Everything below shipped in it.

### 2026-11-04: release tags and published installers
- Release workflow rebuilt: started by a tag or by hand with a dry-run switch, checks the tag against
  `pyproject.toml`, publishes only from `main`, re-runnable. A test keeps `__version__` equal to the
  `pyproject.toml` version.

### 2026-11-04: release assets for installers
- Releases now also publish `requirements.lock`: the runtime dependencies with hashes, which the
  central installer's direct mode installs with `--require-hashes` before adding the wheel.
- Release workflow cleaned up (prerelease flag no longer relies on word splitting); actionlint clean.

### 2026-11-04: container image and CI
- Multi-stage `Dockerfile` (uv, non-root user, health check); `migrate` and `serve` are separate runs.
- GitHub Actions: `ci.yml` (lint, format, strict types, tests on SQLite and PostgreSQL 16, contract
  drift, image build) for PRs and branches; `docker.yml` publishes `latest` + commit SHA for
  `linux/amd64,linux/arm64` on pushes to `main`, the same way `file-node` does; `release.yml`
  builds a versioned image and a GitHub release when a `v*` tag is pushed. Actions are pinned to
  commit SHAs.
- Fixed found by running on PostgreSQL: ASNs are 32-bit, so `nodes.asn` is now a `BIGINT`.
- Test mode for PostgreSQL via `IPPING_TEST_DATABASE_URL`.

### 2026-11-04: measurement engine and API
- Tasks are stored before dispatch; statuses only move forward; disconnects, deadlines, cancellation
  and backend restarts all end in an explicit state.
- REST API, live SSE with `Last-Event-ID` resume, node enrollment and WebSocket, first-run setup,
  admin sign-in with CSRF protection, runtime settings and an emergency stop.
- `contracts/openapi.json` and `node-protocol-v1.schema.json` generated, with a drift test.
- 104 tests; `ruff` and strict `mypy` clean.

### 2026-11-04: data layer
- SQLAlchemy 2 schema (nodes, measurements, per-node tasks, samples, ordered events, audit log).
- Alembic migration shipped inside the package; startup refuses a database that is not at head.
- Test that the migration produces exactly the schema the models describe.

### 2026-11-04: node protocol v1
- Pydantic models for every WebSocket frame, tolerant of unknown fields so v1.x can add optional ones.
- Example frames in `contracts/fixtures/` that the Rust node will parse in its own tests.

### 2026-11-04: destination safety
- Target parsing: IP / hostname / URL in, host out; credentials, paths and query strings are dropped.
- Address policy: private, loopback, link-local, multicast, reserved, NAT64 / 6to4 / Teredo and
  operator-denylisted ranges refused; every resolved address is checked and one is pinned.
- Token hashing, argon2 passwords and in-memory rate limiters.

### 2026-11-04: project skeleton
- Packaging (`uv`, Python 3.12), MIT license, and the plan + roadmap in the README.
