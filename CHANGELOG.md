# Changelog

Progress log for `ipping-backend`, newest first. Each entry is one small, tested step.

## Unreleased

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
