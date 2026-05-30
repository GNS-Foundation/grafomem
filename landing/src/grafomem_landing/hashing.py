"""BLAKE2b hashing + canonical serialization. Mirrors the ID/hash conventions used
in src/aml/provenance.py (fact_id = BLAKE2b-128, 0x1F separator) and the gcrumbs
content/corpus hashing (BLAKE2b-256)."""
import hashlib, json

US = b"\x1f"  # ASCII unit separator — the same delimiter discipline as GMP fact_id


def canon(obj) -> bytes:
    """Deterministic, sorted, separator-stable JSON encoding for hashing/signing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def b2_256(data: bytes) -> str:
    """Content / corpus / Merkle-node hash."""
    return hashlib.blake2b(data, digest_size=32).hexdigest()


def b2_128(*parts: str) -> str:
    """Tenant-scoped identity hash (fact_id / certificate_id style), 0x1F-separated."""
    h = hashlib.blake2b(digest_size=16)
    h.update(US.join(p.encode() for p in parts))
    return h.hexdigest()
