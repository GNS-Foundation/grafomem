import json
import uuid
import os
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption

from aml.cloud.erasure_proof import ErasureProofService, verify_erasure_effect

class MockSigningIdentity:
    def __init__(self, priv):
        self.priv = priv
    def sign(self, digest: bytes):
        sig = self.priv.sign(digest)
        pub_bytes = self.priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return (sig, pub_bytes)

def run():
    print("--- Test 1 & 2: Round Trip & Freshness LIVE ---")
    # Generate keys
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    pub_bytes = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)
    signer = MockSigningIdentity(priv)

    os.environ["GRAFOMEM_SIGNING_KEY"] = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    
    eps = ErasureProofService(db_url="mock")
    # Mock the DB execute path to just return the cert so we don't crash on SQL
    class MockConn:
        def execute(self, *args, **kwargs): pass
        def commit(self): pass
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    eps._get_conn = lambda: MockConn()
    
    # Issue a cert
    tenant_id = "test_tenant"
    fact_ref = 1234
    coverage = {"primary": "absent", "embedding": "absent"}
    
    cert = eps.issue_certificate(
        tenant_id=tenant_id,
        fact_ref=fact_ref,
        fact_content="test_content",
        coverage=coverage,
        signing_identity=signer
    )
    
    cert_data = {
        "certificate_id": cert.certificate_id,
        "tenant_id": cert.tenant_id,
        "fact_ref": cert.fact_ref,
        "fact_content_hash": cert.fact_content_hash,
        "governance_record": cert.governance_record,
        "coverage": cert.coverage,
        "erasure_requested_at": cert.erasure_requested_at.isoformat(),
        "erasure_completed_at": cert.erasure_completed_at.isoformat(),
        "legal_basis": cert.legal_basis,
    }
    
    cert_bytes = json.dumps(cert_data).encode("utf-8")
    required_stores = ["primary", "embedding"]
    
    # 1. Round-trip
    res1 = verify_erasure_effect(cert_bytes, cert.signature, pub_bytes, required_stores)
    print(f"[Test 1] Round-trip valid? (Expected: enforced) -> {res1.get('result')}")
    assert res1.get("result") != "invalid", "Signature verification failed!"
    
    # 2. Freshness LIVE
    record = cert.governance_record
    valid_until_str = record["non_claims"]["freshness"]["valid_until"]
    valid_until_dt = datetime.fromisoformat(valid_until_str.replace("Z", "+00:00"))
    
    stale_time = valid_until_dt + timedelta(days=1)
    res2 = verify_erasure_effect(cert_bytes, cert.signature, pub_bytes, required_stores, current_time=stale_time)
    print(f"[Test 2a] Stale Check (Expected: incomplete) -> Result: {res2.get('result')} | Note: {res2.get('note', '')}")
    assert res2.get("result") == "incomplete" and res2.get("note") == "Freshness expired", "Stale check failed to flag incomplete!"
    
    fresh_time = valid_until_dt - timedelta(days=1)
    res3 = verify_erasure_effect(cert_bytes, cert.signature, pub_bytes, required_stores, current_time=fresh_time)
    print(f"[Test 2b] Fresh Check (Expected: enforced) -> Result: {res3.get('result')}")
    assert res3.get("result") == "enforced", "Fresh check wrongfully flagged as stale or invalid!"
    print("SUCCESS: Freshness and Round-trip tests passed!\n")

if __name__ == "__main__":
    run()
