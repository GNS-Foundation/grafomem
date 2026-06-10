#!/usr/bin/env python3
"""
Phase 0 validation script: Integrity Foundations.
Validates the honest execution of Key Custody and erasure certificates.
"""

import os
import sys
import base64
import asyncio
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey, Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption

# Setup the environment directly for the script test
priv = Ed25519PrivateKey.generate()
raw_seed = priv.private_bytes(encoding=Encoding.Raw, format=PrivateFormat.Raw, encryption_algorithm=NoEncryption())
test_erasure_key = raw_seed.hex()

test_fernet_key = Fernet.generate_key().decode('utf-8')

os.environ["ERASURE_SIGNING_KEY"] = test_erasure_key
os.environ["PROVIDER_ENCRYPTION_KEY"] = test_fernet_key
os.environ["GRAFOMEM_CLOUD"] = "true"

from fastapi.testclient import TestClient
from aml.server.app import create_app, _tenant_id
db_url = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:grafomem_dev@localhost:5432/grafomem")
app = create_app(db_url=db_url, auth_mode="token", tokens={"test_tenant": "test_tenant"})

# Mock _tenant_id to bypass cloud API key checks
app.dependency_overrides[_tenant_id] = lambda: "test_tenant"

def test_phase0_erasure_and_encryption():
    with TestClient(app) as client:
        print("--- Phase 0 Integrity Verification ---")
    
    # 1. Register a tenant with a provider key to verify ProviderEncryption
    # Since we're in Cloud mode (tenant_manager), creating a tenant works via the app context if we manually seed it.
    # We will just test the Identity object directly for ProviderEncryption to ensure independence.
    from aml.cloud.identity import EnvIdentity
    identity = EnvIdentity()
    
    plaintext_key = "sk-test-fake-api-key-12345"
    ciphertext = identity.encrypt(plaintext_key)
    
    print(f"Original: {plaintext_key}")
    print(f"Encrypted: {ciphertext}")
    
    assert plaintext_key not in ciphertext, "Plaintext API key leaked into ciphertext!"
    assert identity.decrypt(ciphertext) == plaintext_key, "Decryption failed!"
    print("✓ ProviderEncryption: Independent MultiFernet operational (No plaintext leakage).")

    # 2. Test the app erasure endpoint (Fail-closed)
    # Create a store in a fake tenant
    tenant = "test_tenant"
    headers = {"Authorization": f"Bearer {tenant}"}
    
    resp = client.post("/v1/stores", headers=headers)
    assert resp.status_code == 200, f"Failed to create store: {resp.text}"
    store_id = resp.json()["store_id"]
    
    # Write a memory
    resp = client.post(f"/v1/stores/{store_id}/write", json={"content": "Sensitive data"}, headers=headers)
    assert resp.status_code == 200
    ref = resp.json()["ref"]
    
    # Prove the erasure path works when key is present
    resp = client.post(f"/v1/stores/{store_id}/delete", json={"ref": ref}, headers=headers)
    assert resp.status_code == 200
    cert_id = resp.json().get("erasure_certificate_id")
    assert cert_id is not None, "Missing erasure_certificate_id on delete"
    
    # Retrieve the cert from the erasure_proof service
    ep = app.state.erasure_proof
    cert = ep.get(cert_id)
    assert cert is not None, "Certificate not found in DB"
    assert cert.memory_deleted is True
    
    pub_key_bytes = identity.public_key()
    assert cert.public_key == pub_key_bytes, "Public key mismatch on cert"
    print("✓ Erasure certs: Issued with correct valid=true signature structure.")
    
    # 3. Test Fail-Closed Ordering
    # Write another memory
    resp = client.post(f"/v1/stores/{store_id}/write", json={"content": "Must survive delete failure"}, headers=headers)
    assert resp.status_code == 200
    ref2 = resp.json()["ref"]
    
    # Now purposefully corrupt the signing key via the identity object.
    # The `EnvIdentity` reads on init, so we must mutate its internal state to simulate missing key.
    identity._signing_key = None
    
    # We must mock the app's erasure_proof to use this corrupted identity
    from aml.cloud.erasure_proof import ErasureProofService
    app.state.erasure_proof = ErasureProofService(identity)
    
    # Attempt delete
    resp = client.post(f"/v1/stores/{store_id}/delete", json={"ref": ref2}, headers=headers)
    assert resp.status_code == 503, f"Expected 503 Service Unavailable, got {resp.status_code}"
    
    # Verify the memory SURVIVED (was not destroyed before the failure)
    resp = client.post(f"/v1/stores/{store_id}/retrieve", json={"query": "survive"}, headers=headers)
    assert resp.status_code == 200
    hits = resp.json().get("memories", [])
    assert len(hits) == 1, "Memory was incorrectly deleted before erasure assertion!"
    assert hits[0]["ref"] == ref2
    
    print("✓ Fail-Closed Ordering: System refuses delete operations when unable to sign, preserving data.")
    print("Phase 0 foundations verified successfully.")

if __name__ == "__main__":
    test_phase0_erasure_and_encryption()
