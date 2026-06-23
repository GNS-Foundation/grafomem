import json
import pytest
from datetime import datetime, timezone

from aml.cloud.erasure_proof import ErasureProofService, verify_erasure_effect, compute_certificate_digest
from aml.provenance import SigningIdentity, sign_provenance

@pytest.fixture
def temp_db_url():
    import os
    url = os.environ.get("GRAFOMEM_DB_URL")
    if not url:
        pytest.skip("GRAFOMEM_DB_URL not set")
    return url

class _MockId:
    def __init__(self, k): self.k = k
    def sign(self, m): 
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        priv = Ed25519PrivateKey.from_private_bytes(self.k)
        return priv.sign(m), priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    def public_key(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return Ed25519PrivateKey.from_private_bytes(self.k).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

def test_erasure_bugfix(temp_db_url):
    import os
    ident = _MockId(b"0" * 32)
    
    svc = ErasureProofService(temp_db_url, signing_identity=ident)
    
    # Drop table so ensure_schema creates it fresh with all columns
    conn = svc._get_conn()
    conn.execute("DROP TABLE IF EXISTS erasure_certificates;")
    
    svc.ensure_schema()
    
    # 1. Test Issue then Verify
    cert = svc.issue_certificate("tenant1", 123, coverage={"primary": "absent"})
    print("Cert issued:", cert.certificate_id)
    
    v = svc.verify_certificate(cert.certificate_id)
    print("Verify new cert:", v)
    assert v["valid"], f"Expected valid, got {v}"

    # 2. Test Offline Verifier (verify_erasure_effect)
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
    cert_bytes = json.dumps(cert_data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    
    effect = verify_erasure_effect(cert_bytes, cert.signature, cert.public_key, ["primary"])
    print("Verify effect:", effect)
    assert effect["result"] == "enforced", f"Expected enforced, got {effect}"
    
    # 3. Test Old Cert (no governance_record)
    old_cert_data = {
        "certificate_id": "old-cert-123",
        "tenant_id": "tenant1",
        "fact_ref": 999,
        "fact_content_hash": None,
        "coverage": {"primary": "absent"},
        "erasure_requested_at": "2026-06-01T00:00:00+00:00",
        "erasure_completed_at": "2026-06-01T00:00:00+00:00",
        "legal_basis": "legacy",
    }
    digest = compute_certificate_digest(old_cert_data)
    sig, pub = ident.sign(digest)
    
    conn = svc._get_conn()
    conn.execute(
        f"INSERT INTO erasure_certificates "
        "(certificate_id, tenant_id, fact_ref, fact_content_hash, "
        " coverage, scrubbed_decision_ids, "
        " erasure_requested_at, erasure_completed_at, "
        " legal_basis, requested_by, "
        " signature, public_key, verified, verification_note) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            "old-cert-123", "tenant1", 999, None,
            json.dumps({"primary": "absent"}), json.dumps([]),
            "2026-06-01T00:00:00+00:00", "2026-06-01T00:00:00+00:00",
            "legacy", "user",
            sig, pub,
            True,
            "legacy"
        ),
    )
    
    v_old = svc.verify_certificate("old-cert-123")
    print("Verify old cert:", v_old)
    assert v_old["valid"], f"Expected old cert valid, got {v_old}"

    print("All tests passed successfully.")
