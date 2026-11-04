"""Random credentials and how they are stored.

Every secret generated here has at least 256 bits of entropy, so a plain SHA-256 is enough to
store it: there is nothing to brute-force, which is why no slow password hash is used.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


def new_secret(prefix: str = "") -> str:
    return f"{prefix}{secrets.token_urlsafe(32)}"


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def secrets_match(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
