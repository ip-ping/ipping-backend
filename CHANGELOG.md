# Changelog

Progress log for `ipping-backend`, newest first. Each entry is one small, tested step.

## Unreleased

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
