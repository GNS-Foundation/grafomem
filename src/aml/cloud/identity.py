"""
GRAFOMEM Cloud Identity — production signing and encryption custody.

Provides abstract interfaces for cryptographic signing and encryption that
allow clean swaps from environment-backed keys to true KMS/HSM backends.
"""

from __future__ import annotations

import os
from typing import Protocol


class SigningIdentity(Protocol):
    """Abstract interface for Ed25519 signing. A real KMS never exports keys,
    so this interface takes the raw payload and returns the signature.
    """

    def sign(self, message: bytes) -> tuple[bytes, bytes]:
        """Sign a message using the bound identity.
        
        Returns
        -------
        tuple[bytes, bytes]
            (signature_bytes, public_key_bytes)
        """
        ...
        
    def public_key(self) -> bytes:
        """Return the 32-byte Ed25519 public key."""
        ...


class ProviderEncryption(Protocol):
    """Abstract interface for encrypting LLM provider keys at rest."""

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext string."""
        ...

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a ciphertext string."""
        ...


class EnvIdentity:
    """An environment-backed identity and encryption provider for Phase 0.
    Reads from GRAFOMEM_SIGNING_KEY and PROVIDER_ENCRYPTION_KEY.
    """

    def __init__(self) -> None:
        self._signing_key = None
        self._fernet = None

        signing_hex = os.environ.get("GRAFOMEM_SIGNING_KEY")
        if signing_hex:
            try:
                self._signing_key = bytes.fromhex(signing_hex)
                if len(self._signing_key) != 32:
                    raise ValueError("GRAFOMEM_SIGNING_KEY must be a 32-byte Ed25519 seed.")
            except ValueError as e:
                raise ValueError(f"Invalid GRAFOMEM_SIGNING_KEY: {e}")

        encryption_key = os.environ.get("PROVIDER_ENCRYPTION_KEY")

        if encryption_key:
            try:
                from cryptography.fernet import Fernet, MultiFernet
                keys = [k.strip() for k in encryption_key.split(",") if k.strip()]
                if not keys:
                    raise ValueError("Empty PROVIDER_ENCRYPTION_KEY.")
                self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])
            except Exception as e:
                raise ValueError(f"Invalid PROVIDER_ENCRYPTION_KEY: {e}")

    # --- SigningIdentity ---

    def sign(self, message: bytes) -> tuple[bytes, bytes]:
        if not self._signing_key:
            raise RuntimeError("Signing identity is not bound (GRAFOMEM_SIGNING_KEY missing).")
        
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat,
        )

        priv = Ed25519PrivateKey.from_private_bytes(self._signing_key)
        signature = priv.sign(message)
        public_key = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return signature, public_key

    def public_key(self) -> bytes:
        if not self._signing_key:
            raise RuntimeError("Signing identity is not bound (GRAFOMEM_SIGNING_KEY missing).")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat,
        )
        priv = Ed25519PrivateKey.from_private_bytes(self._signing_key)
        return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    # --- ProviderEncryption ---

    def encrypt(self, plaintext: str) -> str:
        if not self._fernet:
            raise RuntimeError("Encryption identity is not bound (PROVIDER_ENCRYPTION_KEY missing).")
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        if not self._fernet:
            raise RuntimeError("Encryption identity is not bound (PROVIDER_ENCRYPTION_KEY missing).")
        from cryptography.fernet import InvalidToken
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            raise ValueError("Decryption failures are strictly denied")
