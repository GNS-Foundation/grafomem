"""CGR attestation — the neutral, Foundation-signed artifact GEIANT consumes.

Pure and import-isolated: stdlib only, crypto is *injected* (`signer` / `verify`
callables built in the route layer from the Foundation identity). #4b (GEIANT)
mirrors `verify_attestation` with a dependency-light copy of this logic.

Shape of an attestation::

    {
      # --- signed body (the substantive claim, from to_tiergate) ---
      "agent_handle", "subject_key", "subject_did", "dimension", "tier",
      "cgr_score", "confidence", "n_resolved", "capability_tier", "as_of",
      "last_resolved_at",          # v3 (#2): freshness, signed (most recent resolved outcome | null)
      "rationale",
      "schema": "cgr.attestation.v3",
      "issuer": "gns-foundation",
      "issuer_key_id": <foundation pubkey hex>,
      # --- envelope (NOT signed) ---
      "signature": <hex>,          # Ed25519 over the canonical signed body
      "evidence_ref": <gcrumbs breadcrumb_id | null>,
    }

`signature` commits to everything except `signature`/`evidence_ref`. `evidence_ref`
is a post-hoc audit pointer (the gcrumbs breadcrumb that records this attestation's
fingerprint) and is deliberately outside the signed commitment — it is written
*after* signing, so it cannot be part of what is signed. Tamper-evidence of the
issuance record lives in the gcrumbs chain, not in evidence_ref.
"""
from __future__ import annotations

import hashlib

import rfc8785

CGR_ATTESTATION_SCHEMA = "cgr.attestation.v4"   # v4 EMISSION BUMP (was v3). The v4 body is minted by build_attestation; see CGR_ATTESTATION_SCHEMA_V4 below.
# v4 emission-bump TARGET — DORMANT. The mint below produces the v4 body iff the active schema is v4;
# it is not until CGR_ATTESTATION_SCHEMA above is flipped to this value (a separate production deploy).
CGR_ATTESTATION_SCHEMA_V4 = "cgr.attestation.v4"
ISSUER = "gns-foundation"

# Fields present in the envelope but excluded from the signed / fingerprinted body.
_ENVELOPE_KEYS = ("signature", "evidence_ref")


def _canon(obj) -> bytes:
    """RFC 8785 (JCS) canonicalization — a language-neutral canonical JSON so a
    non-Python verifier (GEIANT, #4b) reproduces the signed bytes byte-for-byte.

    JCS pins the two cross-language traps that plain ``json.dumps`` gets wrong:
    raw UTF-8 (no ``\\uXXXX`` ascii-escaping) and ECMAScript number formatting
    (integer-valued floats like ``6.0`` serialize as ``6``; shortest round-trip
    otherwise), plus lexicographically sorted keys and tight separators. Output is
    byte-identical to the stock JS ``canonicalize`` (JCS) library. All signed-body
    values are JSON-native (str/float/int/None) — ``rfc8785`` raises on anything
    else rather than silently coercing, which is the behaviour we want."""
    return rfc8785.dumps(obj)


def _signed_body(att: dict) -> dict:
    """The subset of an attestation that is signed (everything but the envelope)."""
    return {k: v for k, v in att.items() if k not in _ENVELOPE_KEYS}


def canonical_body(att: dict) -> bytes:
    """Canonical bytes of the signed body — what `signer`/`verify` operate over."""
    return _canon(_signed_body(att))


def attestation_fingerprint(att: dict) -> str:
    """BLAKE2b-256 (64-char hex) of the canonical signed body — a stable id of
    exactly-what-was-issued. Anchored into the gcrumbs chain so the chain is a
    tamper-evident record of the attestation (this is why the separate
    cgr-attestations store is unnecessary for the POC)."""
    return hashlib.blake2b(canonical_body(att), digest_size=32).hexdigest()


def _iso_now() -> str:
    """Mint-time ISO-8601 UTC timestamp (matches scoring._now_iso format)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def build_attestation(tiergate: dict, *, signer, issuer_key_id: str, evidence_ref=None,
                      schema: str | None = None, recorded_at: str | None = None) -> dict:
    """Wrap a `to_tiergate` dict in a Foundation-signed attestation.

    `signer(canonical_bytes) -> sig_hex` is injected (built from the Foundation
    identity in the route layer). `evidence_ref` is the envelope pointer; it does
    not participate in the signature.

    `schema` defaults to the module constant `CGR_ATTESTATION_SCHEMA` (currently v3), so the
    EMISSION BUMP is a single change: flip that constant to v4 and every mint funnelling through
    here emits the v4 body. Callers may also pass `schema=CGR_ATTESTATION_SCHEMA_V4` explicitly
    (tests do, to exercise the dormant v4 path without flipping the constant).

    v4 body (pooled judgment aggregate — the read surface's shape): ADDS `recorded_at` (issue time)
    and `verifiability_tag="judgment"`; keeps everything from `to_tiergate` (scoring_scope,
    as_of, last_resolved_at, agent_handle, …); OMITS the per-record fields `domain`/`decision_date`/
    `backfilled` (the §2.2 pooled-aggregate absence gate); carries no `relates_to` (the continues
    edge is a later step). `recorded_at` may be injected for determinism (goldens/tests); otherwise now.
    """
    schema = schema or CGR_ATTESTATION_SCHEMA
    body = {
        **tiergate,
        "schema": schema,
        "issuer": ISSUER,
        "issuer_key_id": issuer_key_id,
    }
    if schema == CGR_ATTESTATION_SCHEMA_V4:
        body["recorded_at"] = recorded_at or _iso_now()
        body["verifiability_tag"] = "judgment"
    signature = signer(_canon(body))
    return {**body, "signature": signature, "evidence_ref": evidence_ref}


def verify_attestation(att: dict, verify) -> bool:
    """Re-canonicalize the signed body and check the signature via injected
    `verify(message, sig_hex) -> bool`. Pure; no crypto imported here."""
    sig = att.get("signature")
    if not sig:
        return False
    return bool(verify(canonical_body(att), sig))
