# ipping-backend

Python API for **ipping**, a self-hostable distributed network diagnostics service: a user enters an
IP address, hostname or URL, every online node runs a **ping, TCP connect, HTTP request,
traceroute or DNS lookup** against it, and the results stream back live.

This repository is the coordinator. It validates targets, schedules work onto nodes, stores and
aggregates results, estimates where an address appears to be, and owns the contracts the other two
repositories build against.

Current release: **v0.8.0** (database migration head `0011`). Nodes of any earlier version keep
working for ping; TCP, HTTP and traceroute need node v0.3.0 or newer, DNS checks need node v0.4.0, traceroute rounds and certificate checks need node v0.5.0, and DNS over HTTPS and QUIC need node v0.6.0 (DNS over HTTPS to quad9 needs v0.7.0, which speaks HTTP/2); a measurement that asks for either goes only to nodes that have it.

| Repository | Role |
|---|---|
| [`ipping`](https://github.com/ip-ping/ipping) | Next.js frontend, and the installer for the whole central application |
| **`ipping-backend`** (this one) | FastAPI service: REST + SSE for browsers, WebSocket for nodes |
| [`ipping-node`](https://github.com/ip-ping/ipping-node) | Rust measurement node, installed on its own |

## How it works

```text
browser --HTTP/SSE--> backend --WebSocket (opened by the node)--> node --ping/TCP/HTTP/traceroute--> target
                        |
                    PostgreSQL
```

* **Nodes dial out.** Each node opens an authenticated WebSocket to the backend, so a node host
  needs no inbound port. Credentials travel in the `Authorization` header, never in the URL.
* **Work is durable.** A measurement and its per-node tasks are written to the database *before*
  anything is sent. After a restart, tasks whose node had not produced anything are sent again
  and the rest are closed as `interrupted` with what they had, instead of vanishing.
* **Live results.** `GET /api/v1/measurements/{id}/events` is Server-Sent Events. Event ids are
  sequence numbers; reconnecting with `Last-Event-ID` replays what was missed.
* **Offline is not packet loss.** A node that disconnects or misses the deadline ends its task as
  `node_disconnected` / `timed_out`. Only probes that were actually sent can be "lost".
* **Nodes receive typed tasks, never commands.** See `contracts/node-protocol-v1.schema.json`.
  Protocol v1 has stayed v1 while growing (new task kinds, `port`, `server_name`, per-sample
  `detail`): changes are additive, unknown fields are ignored, and a node only gets kinds it
  advertised in its hello.
* **Everything from a node is untrusted.** A sample's `detail` (an HTTP response's timings and
  certificate, a traceroute hop) is parsed against a model for its kind, size-limited, stripped of
  control characters and dropped if it does not fit; a traceroute hop that is not a public address
  is stored as "a private hop" with no address.

## Run it locally

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
export IPPING_DATABASE_URL=sqlite+aiosqlite:///./dev.db   # or postgresql://user:pass@host/db
export IPPING_COOKIE_SECURE=false                         # plain HTTP in development
uv run ipping-backend migrate
uv run ipping-backend serve                               # http://127.0.0.1:8000/api/docs
```

On the first start with no administrator, a one-time setup token is printed to the log (or set
`IPPING_SETUP_TOKEN`). Open the frontend's `/setup` page and use it to create the admin account.

```bash
uv run pytest            # 175 tests on SQLite (the multi-instance ones are skipped)
IPPING_TEST_DATABASE_URL=postgresql://user:pass@localhost/test uv run pytest   # all 187 on PostgreSQL
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run python scripts/export_contracts.py   # regenerate contracts/ after changing the API
```

### Container image

Published to Docker Hub by GitHub Actions on every push to `main` (`latest` and the commit SHA,
`linux/amd64` + `linux/arm64`) and with a version tag on releases. The image runs as a non-root
user; migrating and serving are separate runs so the server never changes the schema on its own:

```bash
docker run --rm -e IPPING_DATABASE_URL=postgresql://user:pass@db/ipping <image> migrate
docker run -d -p 8000:8000 -e IPPING_DATABASE_URL=postgresql://user:pass@db/ipping <image>
```

In normal use you do not run this by hand: the `ipping` repository's installer and compose file
bring up the backend together with the frontend, database and reverse proxy.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `IPPING_DATABASE_URL` | `sqlite+aiosqlite:///./ipping.db` | Use PostgreSQL in production |
| `IPPING_SETUP_TOKEN` | generated at startup | Gate for creating the first administrator |
| `IPPING_PUBLIC_URL` | unset | Public origin, used for the same-origin check on writes |
| `IPPING_COOKIE_SECURE` | `true` | Set `false` only when serving plain HTTP |
| `IPPING_ALLOWED_ORIGINS` | none | Extra origins allowed for browser writes (a JSON list) |
| `IPPING_LOG_LEVEL` / `IPPING_LOG_JSON` | `INFO` / `true` | Logging |
| `IPPING_HOST` / `IPPING_PORT` | `127.0.0.1` / `8000` | Bind address |
| `IPPING_WORKERS` | `1` | Backend processes (more than one needs PostgreSQL) |
| `IPPING_INSTANCE_HEARTBEAT_S` | `3` | How often an instance proves it is alive |
| `IPPING_REAPER_INTERVAL_S` / `IPPING_DISPATCH_POLL_S` | `1` / `1` | How often deadlines are checked and pending tasks are looked for (notifications make both faster) |
| `IPPING_INSTANCE_DEAD_AFTER_S` | `10` | How long without a heartbeat before an instance counts as dead |
| `IPPING_PENDING_GRACE_S` | `5` | How long a task waits for a node that is not connected before it is given up on |
| `IPPING_GEOIP_DIR` | unset | Directory of GeoIP / ASN databases (`.mmdb`); the newest `*city*.mmdb` and `*asn*.mmdb` in it are used. `ipping-backend geoip-update` fills it |
| `IPPING_GEOIP_CITY_DB` / `IPPING_GEOIP_ASN_DB` | unset | Explicit paths to a city and an ASN database instead of a directory |
| `IPPING_MAXMIND_ACCOUNT_ID` / `IPPING_MAXMIND_LICENSE_KEY` | unset | A MaxMind account ID and license key for the GeoLite2 downloads, instead of entering them in the admin page (the page then says they come from the environment and cannot be changed there) |
| `IPPING_MAXMIND_BASE_URL` | `https://download.maxmind.com` | Where the GeoLite2 databases are downloaded from (a mirror); https only |
| `IPPING_GEOFEED_URLS` | unset | Comma-separated https addresses of geofeeds (RFC 8805) for `ipping-backend geofeed-update` to fetch into `IPPING_GEOIP_DIR`; nothing is fetched until that command runs |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Reverse proxies whose `X-Forwarded-*` headers are trusted |

Limits that an operator may need to change while running live in the database and are edited
through `PATCH /api/v1/admin/settings` (or the admin page): the emergency stop
(`measurements_enabled`), per-visitor and per-IP concurrency, creations per minute, probe budgets
(`max_probes_per_measurement`, `max_global_inflight_probes`), the deadline, default and maximum
counts, the target denylist, retention, `enabled_kinds`, `allowed_ports` (for TCP, HTTP and TLS),
`max_redirects`, `traceroute_max_hops` and `task_retries`.

## API at a glance

Interactive docs are at `/api/docs`; the machine-readable contract is `contracts/openapi.json`.

| | |
|---|---|
| `GET /api/v1/session` | start a browser session and get its CSRF token (writes send it as `X-CSRF-Token`) |
| `POST /api/v1/measurements` | start one: `{"target": "example.com:443", "kind": "tcp", "count": 4}`; `kind` is `ping`, `tcp`, `http`, `traceroute`, `dns` or `tls`; also `port` (tcp, http, tls), `record_type`, `resolver` and `transport` (dns), `follow_redirects`, `dns_mode` (`fixed` / `regional`), `family`, `node_ids`, `countries` |
| `GET /api/v1/measurements/{id}` | the measurement with its tasks, samples (`http` or `hop` details) and summaries |
| `GET /api/v1/measurements/{id}/events` | live Server-Sent Events, resumable with `Last-Event-ID` |
| `GET /api/v1/measurements/{id}/location` | the estimated location (ping and TCP) |
| `GET /api/v1/ip`, `GET /api/v1/ip/{address}` | what the IP databases say about the caller's address, or any address: network, country, region, city, position, time zone, AS number and organisation (who may use it, and how often, is a setting; see "IP data") |
| `GET /api/v1/measurements/{id}/path` | a traceroute's routers by address: network (ASN) and place from the GeoIP databases, if loaded (a hint to check against the round-trip time) |
| `POST /api/v1/measurements/{id}/cancel`, `.../share` | cancel; make the link public |
| `GET /api/v1/nodes` | the approved nodes, whether each is online, and the kinds it can run |
| `POST /api/v1/nodes/enroll`, `WS /api/v1/nodes/connect` | what a node uses |
| `/api/v1/admin/...` | nodes (approve, revoke, rotate credential), enrollment tokens, settings, audit, status |
| `GET /api/v1/text?target=...` | with an API key: start a measurement, wait, and get the result as plain text (for `curl` and the command-line client) |
| `/api/v1/admin/ip-data` | the MaxMind account (the key is never sent back), the databases in use, and the lookup API's settings: `GET`, `PUT`, and `POST .../update` to download now |
| `/api/v1/admin/api-keys/...` | API keys for scripts: create (shown once), change quotas, revoke |
| `/api/v1/admin/watches/...` | scheduled checks and alerts: list, create, change, delete, `run`, `test-alert`, `runs` (see below) |
| `GET /api/v1/health/live`, `/ready` | liveness and readiness |

## API keys: scripts without a browser

An administrator creates a key (`POST /api/v1/admin/api-keys`: a name and its quotas) and gets it **once**,
as `ipk_...`; only its SHA-256 is stored (it has 256 bits of entropy, so there is nothing to brute-force). A
script sends `Authorization: Bearer ipk_...` (or `X-API-Key`) and then needs no browser session, no cookie and
no CSRF token:

```sh
curl -s -H "Authorization: Bearer $KEY" "https://ipping.example.com/api/v1/text?target=example.com&kind=dns&record_type=MX"
curl -s -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
     -d '{"target":"example.com:443","kind":"tcp"}' https://ipping.example.com/api/v1/measurements
```

* **Quotas of its own.** Each key may start `per_minute` (30) and `per_day` (1,000) measurements and run as many at
  once as a visitor can; they are counted from the measurements the key made, not from a counter that could
  drift, and the per-address and per-session limits that browsers get do not apply. A key over its quota gets a
  429 saying which limit.
* **A key sees its own.** It can read, list and cancel the measurements it started, and no one else's (another
  key gets the same 404 as for a measurement that does not exist); administrators can open them too.
* **A wrong or revoked key is refused (401), not ignored**, so a script learns at once; something that is not
  shaped like a key is just an anonymous request.
* `GET /api/v1/text` **needs a key and never a session**: it has a side effect, so it must not be something a
  web page can make a visitor's browser do. It waits for the result (`wait`, 1 to 30 seconds, 20 by default) and
  prints a report like `ping` or `traceroute` would (`X-Measurement-Id` and `X-Measurement-Status` come as
  headers; if the measurement is still running the text says so).
* The key's start, the administrator and the time are in the audit trail; the key never is. `last_used_at` is
  kept for the administrator (updated at most once a minute).

## Watches: scheduled checks and alerts

A **watch** is a measurement that runs on a schedule and tells someone when the target stops answering.
Administrators create them (`POST /api/v1/admin/watches`): a name, a kind and target exactly as for a
measurement (a `port`, `record_type`, `resolver`, `count`, `node_ids` ... where the kind takes them), an
interval (at least `min_watch_interval_s`, 60 s by default), and the rules of an alert.

* **A run is judged by what the kind means by an answer.** A reply to a ping or a connection that opened; an
  HTTP answer that is not a 5xx (a refused certificate sends no request, so it is not an answer); a DNS answer
  with `NOERROR` and at least one record (a name that vanished is a failure); a route that reached its
  destination. The run is `ok`, `degraded` (fewer than `min_ok_pct` of the nodes got a good answer, 80% by
  default) or `down` (none did). If the check itself cannot run (no node online, the service restarted) the run
  is an `error`, which says nothing about the target and never raises an alert.
* **Alerts are quiet.** One alert after `alert_after` bad runs in a row (2 by default), none while it stays
  bad, one when the target is good again. A POST to a webhook (JSON: event, watch, target, nodes, message) or
  an [ntfy](https://ntfy.sh) topic (`alert_format: "ntfy"`). A delivery that fails is recorded on the watch
  (`last_alert_error`), never lost silently; `POST .../test-alert` sends a test.
* **The alert address is treated like a target.** The name is resolved on the server, every address must be
  public (set `allow_private_webhooks` for a webhook on your own network, such as a self-hosted ntfy),
  credentials in the address are refused, the connection goes to the address that was checked with the original
  name for Host and TLS, and redirects are not followed. The audit trail never records the address, which may
  hold a token.
* **It works with several backend instances.** Every instance runs the scheduler; a due watch is claimed with a
  conditional update of `next_run_at`, so it runs once per interval however many instances there are, and the
  instance that finalises a measurement judges it. A watch's measurement is owned by nobody (only administrators
  can open it) and each run is kept in short form (`watch_runs`: status, nodes ok, the median, the fastest and
  slowest node) for `watch_history_days` (30 by default).
* Limits are runtime settings: `max_watches` (20), `min_watch_interval_s`, `watch_history_days`,
  `allow_private_webhooks`. A watch is validated when it is saved (a TCP watch without a port, an IP address
  for DNS, a blocked name, a private alert address), so a mistake shows up then and not an hour later.

## Kinds of measurement

| `kind` | What one sample is | Notes |
| --- | --- | --- |
| `ping` | an ICMP echo request | `count` 1 to 10 |
| `tcp` | opening a TCP connection | needs a port (`host:port`, or `port`) from the allowed list; `count` 1 to 10; a refusal keeps its time but is not a success |
| `http` | one request | scheme, port and path come from the URL (query strings are dropped); `follow_redirects` to follow up to 5; timings, status, certificate |
| `tls` | one TLS handshake with no request after it (`transport` `quic` makes it a QUIC handshake over UDP offering `h3`, and the sample says `quic` and the protocol agreed) | the target is a name or an address, with a port from the allowed list (default 443; `host:port`, a URL or `port` give another); the node presents the name (SNI) and checks the certificate against the Mozilla roots. The sample carries the certificate: version, cipher, subject, issuer, validity, whether this node trusts it and why not, the names it is valid for and how many certificates the server sent. A certificate that is not trusted is an `error` (`tls_certificate_invalid`) that keeps all of that. A watch calls it good when it is trusted and has at least 14 days left, and a certificate that is valid but nearly out of time is `degraded`, not `down` |
| `traceroute` | one hop | up to 30 hops, three probes each; `count` 1 to 10 repeats the trace like `mtr` (one more probe per hop and round, a second apart) and a hop then carries how many probes it got (`sent`) and every reply; non-public hops are shown as private; Linux nodes only (rounds need node v0.5.0 or newer) |
| `dns` | one question to one resolver | the target is a name (an IP address or an internal name is refused); `record_type` `A` (default), `AAAA`, `CNAME`, `MX`, `NS`, `TXT` or `SOA`; `resolver` `system` (default: each node's own resolvers, what people near it get) or `cloudflare`, `google`, `quad9` (that public resolver, asked from every node); `transport` `doh` asks cloudflare, google or quad9 over HTTPS (port 443, RFC 8484) instead of UDP (quad9 answers only over HTTP/2, which nodes speak from v0.7.0; older nodes are left out of that question). The backend never resolves the name, so a name that does not exist can be checked |

Each sample is one probe: for `ping` and `tcp` the round trip (a refused TCP connection is an `error`
sample that keeps its time), for `http` the whole request with `detail` (`status_code`, DNS /
connect / TLS / first-byte / total microseconds, server, redirects, `tls` certificate info), and for
`traceroute` one hop (`ttl`, public `ip` or `private`, round trips, `reached`). The probe budget
counts nodes times probes (an HTTP check counts 2, a traceroute counts its hop limit and a third of that again for every extra round, a DNS question
counts 1). A `dns` sample carries `detail` too: the resolver (`system` or its public address), the
response code (`NXDOMAIN` is an answer, not a failure), whether the answer was authoritative or came
over TCP, and every record (name, type, TTL, text). From the node's own resolver, answers that are
private addresses are left out and only counted (`redacted`): that resolver may know names that exist
only inside the operator's network, and the backend removes them again whatever a node sends.

Which kinds a node can run is part of its hello (`capabilities.kinds`); a measurement only goes to
nodes that can run it, and asking for a kind none of the online nodes supports says so. Administrators
can switch kinds off and edit the port list (`enabled_kinds`, `allowed_ports`). Redirects are
re-checked for public addresses on the node at every hop, but the denylist only applies to the first
address, so turn following off (`max_redirects: 0`) if that matters on your deployment.

## Estimated network location (experimental)

`GET /api/v1/measurements/{id}/location` says where the target address *appears* to be, from the
smallest round-trip time each node measured and where those nodes are. Latency is not distance, so the
method is built around what it can and cannot know:

1. **Hard limits.** A node that measured `r` ms heard from the target within `150 * r` km (light
   cannot go faster). Places outside any node's limit are impossible. If two nodes' limits cannot
   overlap, the answer is *inconclusive*: "a single location could not be established; possible
   explanations include anycast, routing differences, or unreliable observations". It never declares
   anycast on its own.
2. **Ranking.** The remaining cities (GeoNames, 3,399 cities of 100,000+ people, see `NOTICE`) are
   scored by how well the times fit an internet path, with the path-length factor (1.2 to 3 times the
   fibre distance) fitted per place. Nodes with unverified coordinates count for less.
3. **Honest output.** The result lists its evidence, the other plausible regions, warnings, and a
   strength label (`low`, `moderate`, `strong`), never a percentage or radius for *this* answer.
   "Insufficient evidence" (fewer than 3 nodes with known coordinates, or no node closer than 40 ms)
   and "inconclusive" are normal answers. It needs a measurement made with "same IP for every node",
   so every node probed the same address.

**How well does it work?** Checked on real measurements, not only synthetic ones. The data
(`calibration/`, collected by `scripts/collect_atlas.py`) is public [RIPE Atlas](https://atlas.ripe.net)
data: the round trip measured from about 1,000 anchors (well-run hosts in data centres, positions
known) to each of 56 anchors in 40 countries, plus pings to anycast root servers. A "fleet" is a
random 4, 8, 16 or 32 of those anchors standing in for an ipping deployment; the estimator is asked
where the target is and the answer is compared with the truth. Targets were split in two: constants
were tuned on one half and the numbers below are the **other, untouched half** (28 anchors, 3,360
fleets; `calibration/result-test-multilateration-4.txt`).

| label | share of answers | within 100 km | within 300 km | within 500 km | within 1,000 km | median error | 9 in 10 within |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `strong` | 12% | 78% | 100% | 100% | 100% | 8 km | 190 km |
| `moderate` | 10% | 13% | 77% | 99% | 100% | 250 km | 360 km |
| `low` | 42% | 5% | 30% | 49% | 75% | 530 km | 1,750 km |
| no answer (closest node over 40 ms) | 36% | | | | | | |

What this says, and does not:

* The error grows with how far the *closest* node is: median 7 km when a node is within 5 ms, 250 km
  within 10 ms, 390 km within 20 ms, 920 km within 40 ms. Past 40 ms answers were usually more than
  1,000 km off, so the estimator now declines to answer (it used to give a place anyway; those
  answers had a median error above 2,000 km).
* Before this calibration (model 3, same held-out half) the median error of all answers was 658 km
  and `strong` was within 100 km only 72% of the time. Fitting the fixed delay of a real path (8 ms,
  not 1 ms) cut the median error for a closest node 10 to 20 ms away from 658 to 391 km (5 to 10 ms:
  326 to 250 km; 20 to 40 ms: 1,257 to 920 km). Withholding answers beyond 40 ms brings the median of
  all answers to 293 km. Only the base delay, the evidence cut-off, the `moderate` rule and the
  contradiction bound changed; the other constants were tried and left alone.
* **Anycast is detected, not guessed.** Across 1,120 random fleets measuring seven anycast root
  servers, 95% were reported as "inconclusive" (nodes that cannot all be close to one place); with 16
  or more nodes it was 558 of 560. The rest were fleets with nodes close together, which see one
  instance and cannot tell. Across 6,720 real unicast fleets (both halves) the same check never fired
  falsely. (The contradiction bound, 110 km per round-trip millisecond, was chosen from the physics
  and the calibration half; the held-out half then confirmed it.)
* The 60 targets are anchors in data centres, mostly in well-connected places; a home connection, a
  mobile network or a CDN edge may behave differently. The random fleets are drawn from an anchor
  pool that is heavy in Europe and North America, and your own nodes will be spread differently: the
  table says how the method behaves, not how your deployment will. Anchor coordinates are city-level.
* The estimator can only name cities of 100,000 people or more, so a target in a small town is never
  closer than the nearest such city (median 7 km here, but it can be far for remote targets).

### A GeoIP database as a second opinion (optional)

Set `IPPING_GEOIP_DIR` (and run `ipping-backend geoip-update`, which downloads the free DB-IP Lite city
and ASN databases and checks them before swapping them in; any MaxMind-format `.mmdb` such as GeoLite2
works too) and a result gains two things, neither of which changes the estimate:

* **The network**: the AS number and organisation of the target (`asn`).
* **A cross-check** (`geoip`): the place the database names, how far it is from the estimated place, and
  a verdict. `ruled_out` means a node with a verified position heard from the address so quickly that the
  database's place is impossible (150 km of distance per round-trip millisecond is a limit of physics,
  and the node is named); `consistent` only means no measurement contradicts it; `not_checked` means no
  node with a verified position took part. Anycast addresses are the usual cause of a ruled-out place:
  a database names one city for an address that answers from many.

The databases are read from disk; a directory is looked at again every few seconds, so a first download after the
server started, and a newer file each month, are picked up without a restart, and nothing is fetched while serving a
request. DB-IP Lite is licensed CC BY 4.0 and the result
carries the attribution it asks for.

### IP data: MaxMind GeoLite2 from the admin page

The simplest source of IP data is a free MaxMind account. Sign up for **GeoLite2** at maxmind.com, make a license key
(Manage license keys), and in the admin page open **IP data** and enter the **account ID** (the number) and the **license
key**. The backend then downloads GeoLite2 City and ASN (Country can be added) and keeps them up to date by itself:

* It needs a directory to keep them in (`IPPING_GEOIP_DIR`: set in the container image, and by `ipping.sh` for a direct
  install). The databases are `GeoLite2-City.mmdb` and so on, and are used in preference to any other file there
  (`geoip-update`'s DB-IP Lite files stay as a fallback).
* A check is made every `maxmind_update_hours` (default 24, from 6 to 168), set on the page, and after a new key is given.
  MaxMind publishes City and Country on Tuesdays and Fridays and ASN on weekdays, and allows 30 downloads a day, so a check
  first asks (a free HEAD request) whether anything newer is published and downloads only if so. "Update now" skips the
  question. A failure is tried again after an hour; a refused key or the daily limit after six, and the page says which.
* Each download is checked: the archive's SHA-256 must match the one MaxMind publishes, only `<edition>.mmdb` is taken out
  of it, and it must open as a database of that edition and answer a lookup before it replaces the old file (an atomic
  rename). The credentials go to MaxMind only (never along its redirect to storage), over https, and are never in an error
  message or a log line. With several backend processes on one host, one downloads (a lock in the directory) and the status
  file there tells the others.
* **The license key is write-only**: the API takes it and never returns it (only whether one is saved), and an audit event
  says that credentials changed, not what they are. It is stored in the database (`app_settings`) like the other settings,
  not encrypted, so protect the database, or give it in the environment instead.

GeoLite2 needs the attribution MaxMind's license asks for, which results and the lookup API carry, and its license does not
let you hand out the databases themselves: this serves single lookups, limited as below, not the files.

**The lookup API.** `GET /api/v1/ip/{address}` (and `GET /api/v1/ip` for the caller's own address, which is the real
visitor's behind a proxy that is set up as the installer does) answers:

```json
{"address": "93.184.216.34", "version": 4, "public": true, "found": true, "network": "93.184.216.0/24",
 "country": "DE", "country_name": "Germany", "continent": "EU", "region": "Hesse", "region_code": "HE",
 "city": "Frankfurt am Main", "postal_code": "60313", "latitude": 50.11, "longitude": 8.68,
 "accuracy_radius_km": 20, "time_zone": "Europe/Berlin", "asn": 15133, "organization": "Edgecast",
 "source": "MaxMind GeoLite2", "database_built": "2026-11-10T00:00:00Z", "attribution": "This product includes GeoLite2 ..."}
```

A private or reserved address is answered with `public: false` and nothing looked up; an address the databases do not know
with `found: false`; text that is not an address is a 422; with no database installed, a 503 that says how to get one. The
admin page sets whether it is **on**, **who may use it** (an API key or an administrator's session by default, or anyone, or
administrators only) and **how many lookups a minute** one client address may make (60 by default; administrators are not
limited). The API keys are the ones for scripts (see above).

**Nodes are filled in from their address.** A node's country, region, city, position, provider and AS number come from the
data for the address it connects from, with one rule: a field an administrator typed (or the node reported when it enrolled)
is never overwritten. A field that was filled automatically follows the data (a new database, a new address); editing a
field makes it yours, and clearing it hands it back. A position taken from the data is a city's, so `location_verified` is
never set by it. Admin nodes carry `auto_fields` (which fields are automatic), and `POST /api/v1/admin/nodes/{id}/autofill`
fills one now. The whole thing can be switched off on the page, and turning it on fills what is empty at once.

### Geofeeds as another claim (optional, opt-in)

A geofeed (RFC 8805) is a CSV a network publishes at a URL of its choosing: `prefix,country,region,city,postal`. It is
the one source of a place that comes from the network's owner, so a result can show it next to the database's. Set
`IPPING_GEOIP_DIR` and list the feeds you want in `IPPING_GEOFEED_URLS` (or give `--url` for each), then run
`ipping-backend geofeed-update` (`ipping.sh geofeed-update --feed https://...` does it for an installation). A result
whose address a feed covers gains `geofeed`: the feed's host, the prefix of the line, what it says in its own words
(`claim`) and, when the gazetteer knows the city (an exact name, or the largest city that begins with it, such as
"Frankfurt" for "Frankfurt am Main"), the same verdict as the database's: `ruled_out` (a node with a verified position heard
from the address too quickly), `consistent`, or `not_checked`. A line with only a country, or a town the gazetteer does not
have, is shown as written and not checked. It never changes the estimate.

It is built so that nothing leaves the server because of a measurement and nothing unbounded comes in:

* **Only the feeds you list are fetched, only when the command runs**, never while serving a request and never because a
  measurement named an address. The result is one file in the GeoIP directory, replaced atomically and read again when it
  changes; a feed that fails keeps the lines it had, and one you stop listing goes.
* https only (a redirect to anything else is refused), a host whose addresses are all public, 32 MB and 30 seconds a feed,
  500,000 lines a feed and 1,000,000 in all (the table is held in memory by every worker).
* A line that is not a valid public prefix (no private ranges, nothing broader than a /8 or a /16 for IPv6 so a feed cannot
  claim the internet), has a malformed country, or says nothing ("do not geolocate") is skipped and counted.

What it is not: **a feed is a claim by the network's operator**. The cross-check can show a claim impossible; it cannot
confirm one. Tried on the real feeds of Cloudflare (151,633 prefixes, 52 "do not geolocate" lines skipped), AS13030 (4,158)
and Telstra Global (1,020): an address in a Cloudflare line is placed in Singapore or Tokyo with coordinates, an AS13030 line
names a Swiss town the gazetteer lacks (shown as the claim), and Telstra's lines are country only.

**Finding feeds is up to you.** A registry can say where a network's feed is (the `geofeed:` attribute in RIPE, APNIC and
AFRINIC data, or a remark in ARIN's), but reading that for every address would send each target to a third party, and reading
it in bulk is large: the RIR databases that carry the attribute are about 340 MB, and in RIPE's IPv6 file alone 599,826
lines name only 2,141 different feeds, 585,735 of them one broker's. Neither is done; list the feeds of the networks you
care about (your own, your providers', the ones you measure).

Anycast addresses, CDN edges and VPN endpoints are located where they answer from, not where their
operator or users are. To redo this: `uv run python scripts/collect_atlas.py` (needs the network) and
`uv run python scripts/evaluate_location.py --set test`.

## Security model

* Targets are reduced to a host; credentials, paths and query strings in a pasted URL are dropped.
* Hostnames are resolved once and **every** address must be public (loopback, private,
  link-local, multicast, reserved, NAT64/6to4/Teredo and denylisted ranges are refused); the
  pinned address is what nodes probe. Nodes repeat the check themselves.
* Browser writes need a matching `Origin` and an `X-CSRF-Token`. Sessions are rotated at sign-in.
* Node credentials and enrollment tokens are 256-bit random values stored only as hashes.
  Enrollment tokens are single-use and expire; new nodes stay `pending` until an admin approves.
* Authentication proves *which* node sent a result, not that it measured honestly.

## Running more than one process

`ipping-backend serve --workers 4` (or `IPPING_WORKERS=4`, or `--backend-workers 4` in the installer)
runs four full backend instances, and instances on other hosts work the same way. They need
PostgreSQL (SQLite refuses more than one), and roughly synchronised clocks (NTP), because "alive" is
a recent heartbeat. Any instance can serve any request, hold any node's connection and stream any
measurement. How:

* **Presence in the database.** Each instance heartbeats a row (`backend_instances`); each node
  connection is a row (`node_connections`) naming the instance holding the socket. A node is online
  if its row exists *and* its instance is alive. A clean shutdown removes the rows at once; a crash is
  noticed after `IPPING_INSTANCE_DEAD_AFTER_S` (10 s).
* **The owner sends the work.** Creating a measurement only writes tasks. The instance holding a
  node's connection claims that node's pending tasks under the measurement's row lock (pending to
  dispatched, recording itself as owner) and then sends them.
* **A reaper in every instance** closes measurements past their deadline, settles tasks whose
  owner instance died (sent again if the node had produced nothing, otherwise interrupted), and
  fails tasks whose node never came back. Nothing depends on the instance
  that created a measurement still being alive.
* **Notifications are hints.** `LISTEN/NOTIFY` wakes other instances (new tasks, new events, commands
  such as "close this socket" or "cancel this task"); every consumer also polls, so a lost
  notification costs milliseconds to seconds, never a result. Live streams tail the event table.
* **Shared limits and setup.** Rate limits are counted in the database; first-run setup is
  race-proof across instances and they agree on the setup token.

Each instance keeps its own database pool (up to 20 connections) and one `LISTEN` connection, so
size PostgreSQL's `max_connections` for roughly 21 per instance.

Tested with real processes: three workers, six real nodes spread across them, every measurement
completing on all nodes whichever worker created it, and a worker holding nodes killed
mid-measurement. Where its tasks had already produced samples the measurement settled as
`interrupted` with exactly those tasks (the others completed, all nodes reconnected). Where they had
not (a TCP check to a port that drops packets), the three tasks were sent again to the reconnected
nodes and the measurement completed in 13.6 s with all six, `attempts` 2 on the three that had been
on the dead worker. The two-instance behaviour is covered by `tests/test_cluster.py`.

## Known limits

* Traceroute needs Linux on the node (it uses the kernel's ICMP error queue).
* A TCP or HTTP check connects to a destination the user chose, from the node's address, on the allowed ports only. Nodes should not be on networks where that is unwelcome.
* Public IPv6 has not been tested end to end (the development machine has no IPv6 internet); IPv6
  targets, IPv6 loopback and an IPv6 traceroute through network namespaces were. (Nor does a GitHub-hosted runner have an IPv6 route: only a link-local address, checked, so CI cannot do it either.)
* The location estimate is only as good as the nodes' coordinates and how close the nearest node is
  (see the table above); it is not a geolocation database. An optional GeoIP database can be set up as a
  cross-check, but it never feeds the estimate. Geofeeds (RFC 8805) are read only from the feeds an administrator lists: finding them for every address would send each target to a third-party RDAP service (of six well-known networks only Cloudflare publishes one, and only as free text in an ARIN remark), and finding them in bulk means about 340 MB of registry data for a few thousand feeds, so the discovery is left to the administrator.
* A task is sent again (once, `task_retries`) only when its backend instance died and the node had
  produced nothing yet. Once a node has produced samples a crash means `interrupted` with the
  partial results kept, because starting over would discard them. A node that drops is never
  retried: it is reported as `node_disconnected`.

## Releases

Tags are `vX.Y.Z` and must match `pyproject.toml`. A release publishes the versioned image, the
wheel, a hash-locked `requirements.lock` (used by the central installer's direct mode), the API and
protocol contracts, and `SHA256SUMS`, with signed build-provenance attestations
(`gh attestation verify`). Releasing is described in
[RELEASING.md](https://github.com/ip-ping/ipping/blob/main/RELEASING.md); the backend is released
first because the application release pins it.

| Release | Database head | Notes |
|---|---|---|
| v0.8.0 | `0011` | IP data: MaxMind GeoLite2 downloaded and kept up to date from the admin page, `GET /api/v1/ip`, and nodes filled in from their address (migration `0011` adds `nodes.auto_fields`) |
| v0.7.0 | `0010` | DNS over HTTPS to Quad9 (over HTTP/2, so only to nodes of v0.7.0 or newer); geofeeds (RFC 8805) from the feeds an administrator lists, shown next to the GeoIP place and checked the same way (`ipping-backend geofeed-update`) |
| v0.6.1 | `0010` | fix: a node older than v0.6.0 is no longer asked for DNS over HTTPS or QUIC, which it answered the plain way without saying so |
| v0.6.0 | `0010` | community nodes (a tier per node and token, left out unless asked for, rate limited, kept out of location estimates until verified, a location audit); DNS over HTTPS and QUIC as transports of the DNS and certificate checks |
| v0.5.1 | `0009` | fix: a backend that was paused (a suspended machine or VM, a long stall, PostgreSQL being away) no longer drops its nodes' connections and leaves them offline |
| v0.5.0 | `0009` | traceroute in rounds like `mtr`; the path endpoint (networks and places of a route's routers); certificate checks (`tls` is enabled for deployments that had every kind on); a fix to the text cleaner |
| v0.4.1, v0.4.0 | `0008` | DNS checks (`dns` is enabled for deployments that had every kind on); GeoIP / ASN cross-check; watches; API keys and the text endpoint |
| v0.3.0 | `0005` | TCP, HTTP and traceroute; tasks sent again when their instance dies; calibrated location model |
| v0.2.0 | `0002` | estimated location; signed releases |
| v0.1.0 | `0001` | first release |

Migrations run as an explicit step (`ipping-backend migrate`); the installer backs the database up
first. Upgrading from v0.2.0 was tested for real, including a v0.2.0 node connecting to the newer
backend and running a ping.

## Community nodes

A node can be **official** (run by the people who run this service) or **community** (run by a volunteer). The point
is to let volunteers add vantage points without letting a stranger's machine speak for the service. This is a first
slice with clear limits, not a trust system. What it does:

* **A volunteer needs a token of their own.** In Admin > Enrollment, create a token with tier *community*; it is single-use
  and labelled with who it is for, so there is always a person behind a node. The node that enrolls with it is a
  community node and still waits for an administrator to approve it. An administrator can change a node's tier.
* **They are left out unless asked for.** "All nodes" means the official ones. A measurement that sets
  `include_community` (the box on the measure form; `include_community=true` on `/api/v1/text`) also uses the online
  community nodes, and choosing a node by name always works. Watches use official nodes only.
* **Results say where they came from.** Every task carries its node's `tier`, shown as a badge in the interface.
* **They cannot move a location estimate** until their place has been checked: a community node whose location is not
  marked verified is not evidence for an estimate, whatever round trip it reports.
* **They are rate limited.** Each community node is asked for at most `community_tasks_per_minute` tasks a minute
  (default 20, an administrator setting): "all nodes" leaves a busy one out, and a request that names it is told so
  (`node_busy`, 429).
* **Their declared place can be audited.** `POST /api/v1/admin/nodes/{id}/audit-location` asks at least three online,
  verified, official nodes to ping the address the node connects from, and applies the physics the location estimate
  uses: no node can be further from an address than 150 km per millisecond of round trip. The result is
  *inconsistent* (a verified node measured it too quickly for the declared place; it is named), *consistent* (not
  contradicted, with a verified node close enough to mean something; this does not prove the place, a node in the next
  city passes too) or *inconclusive* (too few verified nodes, none close enough, or the node connects from an address that
  is not public, such as behind NAT or a VPN). It stores the answer and changes nothing: marking the place verified is the
  administrator's decision.

What it does **not** do, so nobody assumes it:

* **It does not make a volunteer's results trustworthy.** A node can report any round trip, answer, status or
  certificate. Keeping community results out by default, and labelling them when they are in, is the protection; nothing
  checks them. Do not put a community node in a watch, and read its numbers as a volunteer's claim.
* **It does not verify a place.** An audit can contradict a declared place; it cannot prove one.
* **It does not stop a node from being used against its operator.** A node only runs the typed tasks the backend sends,
  against public addresses and allowed ports (the node's own policy), and the operator can narrow that further; but the
  backend, which the operator trusts, chooses the targets. Run a node only for a service you trust.
* **Signed results and reputation are not built, on purpose.** Signing would prove which node sent a result, not that it
  was true, and the backend already knows which node sent it (the node's credential). A reputation needs ground truth to
  score against, and the only ground truth here is the same nodes. A known-answer check against the backend's own address
  would catch a node that does no work at all, and nothing more: a node that recognises the check passes it and lies about
  the rest. Anything that sounded like verification would make a volunteer's numbers look more trustworthy than they are,
  so the protection stays what it is: left out by default, labelled when in, rate limited, never in a watch, never in an
  unverified location estimate.

## Roadmap

Small steps, each one committed and logged in [CHANGELOG.md](CHANGELOG.md).

- [x] Node protocol v1 and contracts (`contracts/`)
- [x] Target parsing and destination policy
- [x] Database schema and migrations
- [x] Measurement engine, REST API, SSE, node WebSocket, admin and setup
- [x] Dockerfile, CI (lint, types, tests, contract-drift check) and Docker Hub publishing
- [x] API tests also run on PostgreSQL
- [x] Release workflow: tag or manual run, dry run, version check, image + wheel + lock + contracts
- [x] Tagged releases: v0.1.0, v0.2.0, v0.3.0, v0.4.0, v0.4.1, v0.5.0, v0.5.1, v0.6.0, v0.6.1, v0.7.0
- [x] More than one backend process or host (shared presence, owner-sends dispatch, reaper)
- [ ] Per-session / per-IP quota tuning from real traffic
- [x] TCP, HTTP and traceroute (protocol, planning, validation, tests; node and frontend shipped in v0.3.0)
- [x] Tasks sent again when their backend instance dies before the node produced anything
- [x] API keys with quotas of their own, and a plain-text endpoint for `curl` (the command-line client is in the ipping repository)
- [x] Scheduled checks (watches) with quiet alerts to a webhook or ntfy, safe with several instances
- [x] DNS checks (record types, public or per-node resolvers, privacy rules; the comparison view is in the web interface)
- [x] Community nodes, first slice: a tier per node and token, left out unless asked for, rate limited, kept out of location estimates until verified, a location audit (no signed results or reputation)
- [x] Traceroute in rounds like `mtr` (loss and spread per hop)
- [x] QUIC as a transport of the certificate check (an h3 handshake over UDP)
- [x] DNS over HTTPS as a transport of the DNS check (cloudflare, google and quad9; quad9 needs HTTP/2, so only nodes of v0.7.0 or newer are asked)
- [x] Certificate checks (`tls`: a handshake with no request; expiry, issuer, names, trust; a watch can alert before it expires)
- [x] The network and place of each router on a traced path (from the GeoIP databases; the route map is in the web interface)
- [x] Every kind over IPv6 with global addresses and the real policy, in a network-namespace lab (node CI); see the node README for what that does and does not prove
- [ ] Over the real public IPv6 internet (needs a host with IPv6 connectivity; neither the development machine nor CI runners have it)
- [x] Estimated network location: constraint-based, evidence listed, explicit "insufficient evidence"
- [x] Calibrate the location model against real targets of known location (RIPE Atlas anchors; held-out half reported)
- [x] MaxMind GeoLite2 from the admin page (account ID and license key, downloaded, checked, kept up to date), a lookup API at `/api/v1/ip` with settings, and nodes filled in from their address
- [x] A GeoIP / ASN database as a checked second opinion (ruled out by the measurements, or consistent), and geofeeds (RFC 8805) from the feeds an administrator lists, checked the same way

## License

MIT, see [LICENSE](LICENSE).
