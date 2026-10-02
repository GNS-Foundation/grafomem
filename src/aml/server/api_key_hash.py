"""Peppered keyed-hash for API keys at rest — HMAC-SHA256(pepper, api_key).

Hash-at-rest scheme (design: grafomem-internal/design/2026-09-19-hash-keys-at-rest.md). API keys are
192-bit random tokens, so a slow KDF buys nothing and would break O(1) auth; a deterministic keyed
hash preserves a single indexed lookup. The **pepper** is a server secret held OUTSIDE the DB
(GRAFOMEM_API_KEY_PEPPER), so a DB-only dump yields hashes that cannot be inverted or forged.

PR 5 (hash-at-rest, final): auth resolves by api_key_hash ONLY and the plaintext is no longer stored.
The pepper is NEVER passed into SQL — hashing happens in-process.
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


def mint_hash(api_key: str, *, key_id: str | None = None) -> bytes:
    """MINT-time hash: HMAC(pepper, api_key), FAIL CLOSED.

    Hash-at-rest PR 5: the server resolves keys by `api_key_hash` ONLY and no longer stores the
    plaintext, so a row minted without a hash could never authenticate. A mint therefore refuses when
    the pepper is unset (PepperMissing, named with the key_id) instead of writing a dead row. The
    pre-PR-5 `best_effort_hash` (NULL on a missing pepper, resolvable via plaintext under dual-read)
    is retired with the plaintext path: the pepper is required at process startup (#190) and here.
    """
    try:
        return compute_api_key_hash(api_key)
    except PepperMissing as e:
        logger.error("api_key mint REFUSED: %s not set — a key without api_key_hash cannot "
                     "authenticate (hash-only auth). key_id=%s", PEPPER_ENV, key_id)
        raise PepperMissing(f"{e} (mint refused: key_id={key_id})") from None
