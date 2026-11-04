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
uv run pytest            # 104 tests
uv run ruff check . && uv run mypy
uv run python scripts/export_contracts.py   # regenerate contracts/ after changing the API
```

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `IPPING_DATABASE_URL` | `sqlite+aiosqlite:///./ipping.db` | Use PostgreSQL in production |
| `IPPING_SETUP_TOKEN` | generated at startup | Gate for creating the first administrator |
| `IPPING_PUBLIC_URL` | unset | Public origin, used for the same-origin check on writes |
| `IPPING_COOKIE_SECURE` | `true` | Set `false` only when serving plain HTTP |
| `IPPING_HOST` / `IPPING_PORT` | `127.0.0.1` / `8000` | Bind address |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Reverse proxies whose `X-Forwarded-*` headers are trusted |

Limits that an operator may need to change while running (emergency stop, per-user and global
probe budgets, target denylist, retention) live in the database and are edited through
`PATCH /api/v1/admin/settings`.

## Security model

* Targets are reduced to a host; credentials, paths and query strings in a pasted URL are dropped.
* Hostnames are resolved once and **every** address must be public (loopback, private,
  link-local, multicast, reserved, NAT64/6to4/Teredo and denylisted ranges are refused); the
  pinned address is what nodes probe. Nodes repeat the check themselves.
* Browser writes need a matching `Origin` and an `X-CSRF-Token`. Sessions are rotated at sign-in.
* Node credentials and enrollment tokens are 256-bit random values stored only as hashes.
  Enrollment tokens are single-use and expire; new nodes stay `pending` until an admin approves.
* Authentication proves *which* node sent a result, not that it measured honestly.

## Known limits

* **One process.** Node connections and live-stream subscribers are in memory, so run exactly one
  backend process (`serve` enforces `workers=1`). Scaling out needs shared routing first.
* Only ICMP ping exists so far; location estimation is not implemented.

## Roadmap

Small steps, each one committed and logged in [CHANGELOG.md](CHANGELOG.md).

- [ ] Node protocol v1 and contracts (`contracts/`)
- [x] Target parsing and destination policy
- [x] Database schema and migrations
- [ ] Measurement engine, REST API, SSE, node WebSocket, admin and setup
- [ ] Dockerfile and CI (lint, types, tests, contract-drift check)
- [ ] Versioned release artifacts (container image, wheel) and checksums
- [ ] Postgres integration test in CI
- [ ] Per-session / per-IP quota tuning from real traffic
- [ ] TCP, HTTP and traceroute probes
- [ ] DNS comparison across nodes
- [ ] Estimated network location (evidence-based, with explicit "insufficient evidence")

## License

MIT, see [LICENSE](LICENSE).
