"""Peppered keyed-hash for API keys at rest — HMAC-SHA256(pepper, api_key).

Hash-at-rest scheme (design: grafomem-internal/design/2026-09-19-hash-keys-at-rest.md). API keys are
192-bit random tokens, so a slow KDF buys nothing and would break O(1) auth; a deterministic keyed
hash preserves a single indexed lookup. The **pepper** is a server secret held OUTSIDE the DB
(GRAFOMEM_API_KEY_PEPPER), so a DB-only dump yields hashes that cannot be inverted or forged.

DARK in PR 2: this computes/stores the hash (column + backfill); auth still resolves by plaintext
until the dual-read PR. The pepper is NEVER passed into SQL — hashing happens in-process.
"""
from __future__ import annotations

import hashlib
import hmac
import os

PEPPER_ENV = "GRAFOMEM_API_KEY_PEPPER"


class PepperMissing(RuntimeError):
    """Raised when the pepper is absent/empty — hashing must FAIL CLOSED, never silently proceed."""


def get_pepper() -> str:
    """The API-key pepper, or raise. FAIL CLOSED: an absent/empty pepper is refused — hashing under
    an empty pepper is a silent catastrophic failure (every hash wrong, found only at first auth)."""
    pepper = os.environ.get(PEPPER_ENV, "")
    if not pepper:
        raise PepperMissing(
            f"{PEPPER_ENV} is unset or empty — refusing to hash. It is a server secret (same class "
            f"as GRAFOMEM_MASTER_KEY); set it before any backfill or hash write."
        )
    return pepper


def compute_api_key_hash(api_key: str, pepper: str | None = None) -> bytes:
    """HMAC-SHA256(pepper, api_key) as raw bytes. Reads the pepper (fail-closed) if not passed."""
    if pepper is None:
        pepper = get_pepper()
    if not pepper:
        raise PepperMissing(f"{PEPPER_ENV} empty")
    return hmac.new(pepper.encode("utf-8"), api_key.encode("utf-8"), hashlib.sha256).digest()
