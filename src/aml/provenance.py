"""
GRAFOMEM provenance primitives (GMP §4.1) — content-store cryptographic commitment.

The spec's *structured* fact_id is BLAKE2b-128 over (predicate, subject, object,
valid_from) — see aml.generator.trace.compute_fact_id. A *content store* (the GMP
reference and SQLite backends) holds verbalized content, not (P, S, O) triples, and
its write() never receives the triple — so its cryptographic commitment is over the
unit it actually stores: the content and its tenant. fact_id_for_content() is that
content-store analog: the 16-byte value a CRYPTOGRAPHIC_PROVENANCE backend signs and
that interface.verify_provenance checks.

valid_from is deliberately NOT in the commitment. Provenance must be verifiable from
the *retrieved* memory; the store keeps valid_from as an epoch REAL, which round-trips
with float noise that would break byte-exact verification, whereas content and tenant
round-trip exactly as TEXT. Versions are already distinguished by differing content, so
valid_from is not needed for identity here. (Including it would require persisting it at
full precision purely for the commitment — a documented refinement, not v0.2.)

Ed25519 throughout, matching interface.verify_provenance. Requires grafomem[crypto].
"""

from __future__ import annotations

import hashlib

# Re-export so callers have one provenance import surface (verify lives in the
# interface as the canonical reader; sign lives here as its counterpart).
from aml.backends.interface import verify_provenance  # noqa: F401

FACT_ID_BYTES = 16          # BLAKE2b-128, matching trace.FACT_ID_BYTES and §4.1
_SEP = b"\x1f"              # ASCII unit separator (same delimiter as trace canonicalization)


def fact_id_for_content(content: str, tenant_id: str | None) -> bytes:
    """The 16-byte content-store commitment: BLAKE2b-128(content ‖ sep ‖ tenant).

    This is what a CRYPTOGRAPHIC_PROVENANCE content store signs and what
    verify_provenance() is checked against. None tenant encodes as "" so the
    single-tenant and explicit-tenant cases are distinct and stable.
    """
    h = hashlib.blake2b(digest_size=FACT_ID_BYTES)
    h.update(content.encode("utf-8"))
    h.update(_SEP)
    h.update((tenant_id or "").encode("utf-8"))
    return h.digest()


from aml.cloud.identity import SigningIdentity

def sign_provenance(signing_identity: SigningIdentity, fact_id: bytes) -> tuple[bytes, bytes]:
    """Ed25519-sign a 16-byte fact_id. `signing_identity` provides the sign() interface
    (WriteOptions.signing_identity). Returns (signature, public_key) — 64 and 32 raw
    bytes — for SourceMeta. The counterpart to interface.verify_provenance.
    """
    return signing_identity.sign(fact_id)


# ============================================================================
# Decision Trail provenance — inference decisions, not just memory facts.
#
# The same BLAKE2b-128 + Ed25519 pattern, but the commitment is over
# canonical decision fields: (tenant, query, model, output, timestamp).
# ============================================================================

def decision_id_for_record(
    tenant_id: str,
    query: str,
    model_id: str,
    raw_output: str,
    created_at: "datetime",
) -> bytes:
    """16-byte BLAKE2b commitment for a decision record.

    Deterministic: the same inputs always produce the same ID.
    Content-sensitive: any field change produces a different ID.

    Parameters match the mandatory fields of a DecisionRecord — the
    minimal set that uniquely identifies one inference decision.
    """
    h = hashlib.blake2b(digest_size=FACT_ID_BYTES)
    for part in [tenant_id, query, model_id, raw_output, created_at.isoformat()]:
        h.update(part.encode("utf-8"))
        h.update(_SEP)
    return h.digest()


def sign_decision(signing_identity: SigningIdentity, decision_id: bytes) -> tuple[bytes, bytes]:
    """Ed25519-sign a decision_id. Same interface as sign_provenance.

    ``signing_identity`` provides the sign() interface.
    Returns (signature, public_key) — 64 and 32 raw bytes.
    """
    return sign_provenance(signing_identity, decision_id)


# ============================================================================
# Self-test — `python -m aml.provenance`
#   Confirms: the cryptography dep is present; fact_id is deterministic and
#   content/tenant-sensitive; sign -> verify True; tamper -> verify False.
# ============================================================================

if __name__ == "__main__":
    from datetime import datetime, timezone

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat,
    )

    from aml.backends.interface import Memory, SourceMeta

    print("GRAFOMEM provenance primitives — content fact_id + Ed25519 sign/verify\n")

    # 1. fact_id determinism + sensitivity
    fid = fact_id_for_content("Aria lives in Rome", "A")
    assert fid == fact_id_for_content("Aria lives in Rome", "A") and len(fid) == 16
    assert fid != fact_id_for_content("Aria lives in Milan", "A")     # content matters
    assert fact_id_for_content("x", None) != fact_id_for_content("x", "A")  # tenant matters
    print("✓ fact_id_for_content   deterministic, 16 bytes, content + tenant sensitive")

    # 2. sign -> verify True
    seed = Ed25519PrivateKey.generate().private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    
    class _MockIdentity:
        def sign(self, message: bytes):
            priv = Ed25519PrivateKey.from_private_bytes(seed)
            return priv.sign(message), priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        def public_key(self):
            priv = Ed25519PrivateKey.from_private_bytes(seed)
            return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            
    sig, pub = sign_provenance(_MockIdentity(), fid)
    assert len(sig) == 64 and len(pub) == 32
    m = Memory(ref=1, content="Aria lives in Rome",
               written_at=datetime.now(timezone.utc), tenant_id="A",
               source=SourceMeta(signature=sig, public_key=pub))
    assert verify_provenance(m, fid) is True
    print("✓ sign -> verify True    (Ed25519 over the content fact_id)")

    # 3. tamper -> verify False (a fact_id for altered content won't match the signature)
    assert verify_provenance(m, fact_id_for_content("Aria lives in Milan", "A")) is False
    print("✓ tamper -> verify False (signature binds to the exact content + tenant)")

    # 4. missing signature -> False (Check P)
    assert verify_provenance(
        Memory(ref=2, content="x", written_at=datetime.now(timezone.utc)), fid) is False
    print("✓ no signature -> False  (Check P fails cleanly)")

    print("\nProvenance primitives green. Ready to thread through the backends.")
