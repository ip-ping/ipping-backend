# Changelog

Progress log for `ipping-backend`, newest first. Each entry is one small, tested step.

## Unreleased

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
- Needs node v0.4.1 or newer to repeat; an older node ignores `count` for a traceroute and traces once.

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
