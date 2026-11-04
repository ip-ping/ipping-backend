# Changelog

Progress log for `ipping-backend`, newest first. Each entry is one small, tested step.

## Unreleased

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
