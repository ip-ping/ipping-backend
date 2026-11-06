# ipping-backend

Python API for **ipping**, a self-hostable distributed network diagnostics service: a user enters an
IP address, hostname or URL, every online node pings it, and the results stream back live.

This repository is the coordinator. It validates targets, schedules work onto nodes, stores and
aggregates results, and owns the contracts the other two repositories build against.

| Repository | Role |
|---|---|
| [`ipping`](https://github.com/ip-ping/ipping) | Next.js frontend, and the installer for the whole central application |
| **`ipping-backend`** (this one) | FastAPI service: REST + SSE for browsers, WebSocket for nodes |
| [`ipping-node`](https://github.com/ip-ping/ipping-node) | Rust measurement node, installed on its own |

## How it works

```text
browser --HTTP/SSE--> backend --WebSocket (opened by the node)--> node --ICMP--> target
                        |
                    PostgreSQL
```

* **Nodes dial out.** Each node opens an authenticated WebSocket to the backend, so a node host
  needs no inbound port. Credentials travel in the `Authorization` header, never in the URL.
* **Work is durable.** A measurement and its per-node tasks are written to the database *before*
  anything is sent. After a restart, unfinished measurements are closed as `interrupted` instead
  of vanishing.
* **Live results.** `GET /api/v1/measurements/{id}/events` is Server-Sent Events. Event ids are
  sequence numbers; reconnecting with `Last-Event-ID` replays what was missed.
* **Offline is not packet loss.** A node that disconnects or misses the deadline ends its task as
  `node_disconnected` / `timed_out`. Only echo requests that were actually sent can be "lost".
* **Nodes receive typed tasks, never commands.** See `contracts/node-protocol-v1.schema.json`.

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
uv run pytest            # 104 tests (SQLite)
IPPING_TEST_DATABASE_URL=postgresql://user:pass@localhost/test uv run pytest tests/test_api.py  # PostgreSQL
uv run ruff check . && uv run mypy
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
| `IPPING_HOST` / `IPPING_PORT` | `127.0.0.1` / `8000` | Bind address |
| `IPPING_WORKERS` | `1` | Backend processes (more than one needs PostgreSQL) |
| `IPPING_INSTANCE_DEAD_AFTER_S` | `10` | How long without a heartbeat before an instance counts as dead |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Reverse proxies whose `X-Forwarded-*` headers are trusted |

Limits that an operator may need to change while running (emergency stop, per-user and global
probe budgets, target denylist, retention) live in the database and are edited through
`PATCH /api/v1/admin/settings`.

## Kinds of measurement

| `kind` | What one sample is | Notes |
| --- | --- | --- |
| `ping` | an ICMP echo request | `count` 1 to 10 |
| `tcp` | opening a TCP connection | needs a port (`host:port`, or `port`) from the allowed list; `count` 1 to 10; a refusal keeps its time but is not a success |
| `http` | one request | scheme, port and path come from the URL (query strings are dropped); `follow_redirects` to follow up to 5; timings, status, certificate |
| `traceroute` | one hop | up to 30 hops, three probes each; non-public hops are shown as private; Linux nodes only |

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
   heuristic strength (`low`, `moderate`, `strong`), never a percentage or an accuracy radius, which
   would need calibration. "Insufficient evidence" (fewer than 3 nodes with known coordinates, or no
   node closer than 100 ms) is a normal answer. It needs a measurement made with "same IP for every
   node", so every node probed the same address.

**How well does it work?** Only measured on synthetic paths so far (random targets, random fleets of
4 to 32 nodes, path factors 1.2 to 2.8, jitter): across 399 trials every `strong` answer was within
218 km and every `moderate` within 272 km; `low` answers had a median error of 352 km, 58% within 500 km
and a worst case of 8,232 km. That says the arithmetic is sound and the labels mean something; it says
nothing about real-world accuracy, which needs targets of known location (see the roadmap). Anycast
addresses, CDN edges and VPN endpoints are located where they answer from, not where their operator or
users are.

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
* **A reaper in every instance** closes measurements past their deadline, interrupts tasks whose
  owner instance died, and fails tasks whose node never came back. Nothing depends on the instance
  that created a measurement still being alive.
* **Notifications are hints.** `LISTEN/NOTIFY` wakes other instances (new tasks, new events, commands
  such as "close this socket" or "cancel this task"); every consumer also polls, so a lost
  notification costs milliseconds to seconds, never a result. Live streams tail the event table.
* **Shared limits and setup.** Rate limits are counted in the database; first-run setup is
  race-proof across instances and they agree on the setup token.

Each instance keeps its own database pool (up to 20 connections) and one `LISTEN` connection, so
size PostgreSQL's `max_connections` for roughly 21 per instance.

Tested with real processes: three workers, six real nodes spread across them, every measurement
completing on all nodes whichever worker created it, and the worker holding four nodes killed
mid-measurement (the measurement settled as `interrupted` with exactly those four tasks, the other
two completed, and all nodes reconnected). The two-instance behaviour is covered by
`tests/test_cluster.py`.

## Known limits

* Traceroute needs Linux on the node (it uses the kernel's ICMP error queue).
* A TCP or HTTP check connects to a destination the user chose, from the node's address, on the allowed ports only. Nodes should not be on networks where that is unwelcome.
* Task delivery is at most once per attempt: a task claimed by an instance that then dies is
  interrupted, not retried on another node connection.

## Releases

Tags are `vX.Y.Z` and must match `pyproject.toml`. A release publishes the versioned image, the
wheel, a hash-locked `requirements.lock` (used by the central installer's direct mode), the API and
protocol contracts, and `SHA256SUMS`. Releasing is described in
[RELEASING.md](https://github.com/ip-ping/ipping/blob/main/RELEASING.md); the backend is released
first because the application release pins it.

## Roadmap

Small steps, each one committed and logged in [CHANGELOG.md](CHANGELOG.md).

- [x] Node protocol v1 and contracts (`contracts/`)
- [x] Target parsing and destination policy
- [x] Database schema and migrations
- [x] Measurement engine, REST API, SSE, node WebSocket, admin and setup
- [x] Dockerfile, CI (lint, types, tests, contract-drift check) and Docker Hub publishing
- [x] API tests also run on PostgreSQL
- [x] Release workflow: tag or manual run, dry run, version check, image + wheel + lock + contracts
- [x] First tagged release: v0.1.0
- [x] More than one backend process or host (shared presence, owner-sends dispatch, reaper)
- [ ] Per-session / per-IP quota tuning from real traffic
- [x] TCP, HTTP and traceroute: backend side (protocol, planning, validation, tests)
- [ ] TCP, HTTP and traceroute: node side and frontend
- [ ] DNS comparison across nodes
- [x] Estimated network location: constraint-based, evidence listed, explicit "insufficient evidence" (checked on synthetic paths only)
- [ ] Calibrate the location model against real targets of known location (and optionally GeoIP / ASN / geofeed evidence)

## License

MIT, see [LICENSE](LICENSE).
