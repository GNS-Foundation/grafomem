#!/usr/bin/env python3
"""
Generate cryptographic keys for Grafomem Phase 0 environments.

Outputs:
  1. A 32-byte Ed25519 private seed (hex-encoded) for ERASURE_SIGNING_KEY.
  2. A 32-byte URL-safe base64-encoded string for PROVIDER_ENCRYPTION_KEY.
"""

import os
import secrets
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

def main():
    print("GRAFOMEM PROD KEY GENERATOR")
    print("-" * 50)
    
    # 1. Erasure Signing Key (Ed25519)
    # The cryptography library can generate the private key directly.
    priv = Ed25519PrivateKey.generate()
    raw_seed = priv.private_bytes(
        encoding=Encoding.Raw,
        format=PrivateFormat.Raw,
        encryption_algorithm=NoEncryption()
    )
    erasure_key_hex = raw_seed.hex()
    
    print("\n[Erasure Signing Key]")
    print("Used to sign hard-delete cryptographic proofs.")
    print(f"ERASURE_SIGNING_KEY={erasure_key_hex}")
    
    # 2. Provider Encryption Key (Fernet)
    fernet_key = Fernet.generate_key().decode('utf-8')
    
    print("\n[Provider Encryption Key]")
    print("Used to encrypt LLM API keys at rest in the database.")
    print(f"PROVIDER_ENCRYPTION_KEY={fernet_key}")
    
    print("\n" + "-" * 50)
    print("Copy these into your .env.prod or container secrets manager.")

if __name__ == "__main__":
    main()
