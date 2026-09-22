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
import logging
import os

logger = logging.getLogger("grafomem.auth")

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


def best_effort_hash(api_key: str, *, key_id: str | None = None) -> bytes | None:
    """MINT-time hash: HMAC(pepper, api_key) when the pepper is set, else None (the row's
    `api_key_hash` stays NULL — resolvable via plaintext under dual-read, and re-hashed by the next
    pre-deploy backfill).

    Best-effort BY DESIGN: a mint must NOT fail because the pepper is unset — fail-closing here would
    break tenant/key creation in every pepper-less env (local dev, CI, self-host). That differs from
    the bulk BACKFILL (fail-closed): a NULL writes no hash at all (the benign "not yet hashed" state),
    whereas bulk-hashing under an empty pepper would write many identical WRONG hashes. Valid only
    during the dual-read window — after the plaintext column drops (PR 5) a NULL-hash key cannot
    authenticate, so the pepper must be present at startup by then.

    The NULL fallback logs a **WARNING** with `key_id` (same visibility as a PLAINTEXT-PATH resolution)
    so an unset pepper in an env that expects hashing (staging/prod) is never silent.
    """
    try:
        return compute_api_key_hash(api_key)
    except PepperMissing:
        logger.warning(
            "api_key mint: NULL api_key_hash — %s not set; key not hash-resolvable until the next "
            "backfill (DARK, resolves via plaintext under dual-read). key_id=%s", PEPPER_ENV, key_id)
        return None
