"""CGR Foundation issuer identity — the *neutral* signing key.

The whole point of CGR-as-a-product is that the reputation is signed by a key
that is **distinct** from the commercial `signing_identity` that signs an agent's
own decisions & receipts (gcrumbs / R1–R5). Signing reputation with the same key
that signs the agent's work recreates "your own credit bureau" at the key level.
This module loads a separate Ed25519 identity from `FOUNDATION_SIGNING_SEED`.

Import-isolation: this file uses the Ed25519 *primitive* directly (via
`cryptography`), NOT `aml.provenance` / `aml.cloud.*`, so `src/aml/cgr/` stays
decoupled from the cloud layer. The pure attestation logic (attestation.py) is
crypto-agnostic and receives the `signer` / `verify` callables built here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

FOUNDATION_SEED_ENV = "FOUNDATION_SIGNING_SEED"
ISSUER = "gns-foundation"


@dataclass(frozen=True)
class FoundationIdentity:
    """An Ed25519 identity built from a 32-byte seed. Mirrors the SigningIdentity
    shape (`.sign(msg) -> (sig, pub)`, `.public_key() -> pub`) without importing it,
    so cgr stays import-isolated."""

    _seed: bytes

    def sign(self, message: bytes) -> tuple[bytes, bytes]:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        priv = Ed25519PrivateKey.from_private_bytes(self._seed)
        sig = priv.sign(message)
        pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return sig, pub

    def public_key(self) -> bytes:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        priv = Ed25519PrivateKey.from_private_bytes(self._seed)
        return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def load_foundation_identity() -> Optional[FoundationIdentity]:
    """Load the Foundation identity from env `FOUNDATION_SIGNING_SEED` (32-byte hex).

    Returns None if the seed is absent or malformed — callers translate that into a
    503. NEVER falls back to GRAFOMEM_SIGNING_KEY / ERASURE_SIGNING_KEY: the
    neutrality of the seam depends on this key being distinct.
    """
    seed_hex = os.environ.get(FOUNDATION_SEED_ENV)
    if not seed_hex:
        return None
    try:
        seed = bytes.fromhex(seed_hex.strip())
    except ValueError:
        return None
    if len(seed) != 32:
        return None
    return FoundationIdentity(seed)


def issuer_key_id(identity: FoundationIdentity) -> str:
    """The Foundation public key hex — the kid a verifier (GEIANT) pins."""
    return identity.public_key().hex()


def make_signer(identity: FoundationIdentity) -> Callable[[bytes], str]:
    """Build the injected signer: canonical_bytes -> signature hex."""

    def _sign(message: bytes) -> str:
        sig, _pub = identity.sign(message)
        return sig.hex()

    return _sign


def make_verifier(public_key: bytes) -> Callable[[bytes, str], bool]:
    """Build the injected verifier: (message, sig_hex) -> bool. Pure Ed25519 verify
    against a pinned public key. This is what #4b (GEIANT) mirrors."""

    def _verify(message: bytes, sig_hex: str) -> bool:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        try:
            pub = Ed25519PublicKey.from_public_bytes(public_key)
            pub.verify(bytes.fromhex(sig_hex), message)
            return True
        except (InvalidSignature, ValueError):
            return False

    return _verify
