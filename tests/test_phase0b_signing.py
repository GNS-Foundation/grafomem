import os
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

import pytest
from datetime import datetime, timezone
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from aml.cloud.identity import EnvIdentity
from aml.cloud.gcrumbs import GcrumbsService
from aml.cloud.artifact_registry import ArtifactRegistryService
from aml.cloud.landing_service import LandingService
from aml.cloud.erasure_proof import ErasureProofService

# Mocks

@pytest.fixture
def db_url():
    url = os.environ.get("GRAFOMEM_DB_URL")
    if not url:
        pytest.skip("GRAFOMEM_DB_URL not set")
    return url

@pytest.fixture
def clean_tenant(db_url):
    """Ensure test tenant is clean-slated for rotation."""
    import psycopg
    tenant_id = "test_tenant_rotation"
    with psycopg.connect(db_url, autocommit=True) as conn:
        for table in ["erasure_certificates", "gcrumbs_epochs", "gcrumbs_breadcrumbs", "artifact_registry", "landing_certificates"]:
            try:
                conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant_id,))
            except psycopg.errors.UndefinedTable:
                pass
    return tenant_id

def test_verify_all_four_artifacts_and_rotation(db_url, clean_tenant):
    """
    Test Phase 0b objective:
    1. Verify decisions (breadcrumbs), receipts (artifacts), epochs, certs
    2. Confirm verification uses the STORED per-artifact pubkey, not the current identity's.
    """
    import os
    
    # 1. First Identity (Key A)
    key_a = os.urandom(32)
    id_a = _MockId(key_a)
    pub_a = id_a.public_key()
    
    tenant_id = clean_tenant
    
    from aml.cloud.artifact_registry import ArtifactRegisterRequest
    
    # Setup services with Key A
    ar = ArtifactRegistryService(db_url, signing_identity=id_a)
    ar.ensure_schema()
    
    gc = GcrumbsService(db_url, signing_identity=id_a)
    gc.ensure_schema()
    
    ep = ErasureProofService(db_url, signing_identity=id_a)
    ep.ensure_schema()
    
    # Generate Artifact 1 (Receipt) with Key A
    req_a = ArtifactRegisterRequest(
        artifact_ref="test_doc",
        base_model_ref="test_model",
        layers=[{"digest": "hash1", "media_type": "text/plain", "size": 10}],
        metadata={"some": "data"}
    )
    receipt_a = ar.register(tenant_id, req_a)
    
    # Generate Breadcrumb (Decision) with Key A
    bc_a = gc.append_breadcrumb(tenant_id, "test_decision", {"allowed": True}, source_type="test", source_ref="x")
    
    # Generate Epoch with Key A
    epoch_a = gc.roll_epoch(tenant_id)
    
    # Generate Erasure Cert with Key A
    cert_a = ep.issue_certificate(tenant_id, 999)
    
    # Verify with Key A
    assert ar.verify(tenant_id, receipt_a["artifact_id"])["passed"] is True
    assert gc.verify_chain(tenant_id)["status"] == "intact"
    assert ep.verify_certificate(cert_a.certificate_id)["valid"] is True
    # decisions are verified inherently in epoch inclusions or via manual Ed25519 verify
    
    # 2. Key Rotation (Key B)
    key_b = os.urandom(32)
    id_b = _MockId(key_b)
    pub_b = id_b.public_key()
    
    # Ensure keys are different
    assert pub_a != pub_b
    
    # Setup services with Key B
    ar_b = ArtifactRegistryService(db_url, signing_identity=id_b)
    gc_b = GcrumbsService(db_url, signing_identity=id_b)
    ep_b = ErasureProofService(db_url, signing_identity=id_b)
    
    # 3. Prove that Key B can verify Artifacts signed by Key A
    # If it was relying on the LIVE identity instead of the STORED public key, this would fail!
    assert ar_b.verify(tenant_id, receipt_a["artifact_id"])["passed"] is True, "Receipt verification must use stored key"
    assert gc_b.verify_chain(tenant_id)["status"] == "intact", "Chain verification must use stored key"
    assert ep_b.verify_certificate(cert_a.certificate_id)["valid"] is True, "Erasure verification must use stored key"
    
    # 4. Generate new Artifact 2 with Key B
    req_b = ArtifactRegisterRequest(
        artifact_ref="test_doc_2",
        base_model_ref="test_model_2",
        layers=[{"digest": "hash2", "media_type": "text/plain", "size": 10}],
        metadata={"some": "data2"}
    )
    receipt_b = ar_b.register(tenant_id, req_b)
    epoch_b = gc_b.roll_epoch(tenant_id)
    
    assert ar_b.verify(tenant_id, receipt_b["artifact_id"])["passed"] is True
    assert gc_b.verify_chain(tenant_id)["status"] == "intact"
    
    assert bytes.fromhex(ar_b.get(tenant_id, receipt_b["artifact_id"])["document"]["signer_public_key"]) == pub_b
    assert bytes.fromhex(ar_b.get(tenant_id, receipt_a["artifact_id"])["document"]["signer_public_key"]) == pub_a

def test_env_identity_sign():
    """Verify that the real EnvIdentity properly signs using Ed25519."""
    import os
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from aml.cloud.identity import EnvIdentity
    
    # Setup real environment
    seed = os.urandom(32)
    os.environ["GRAFOMEM_SIGNING_KEY"] = seed.hex()
    
    # Calculate expected public key
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    expected_pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    
    # Instantiate EnvIdentity and sign a message
    identity = EnvIdentity()
    message = b"test message for EnvIdentity"
    signature, public_key = identity.sign(message)
    
    # Verify the identity produced the right public key
    assert public_key == expected_pub
    
    # Verify the signature is cryptographically valid under the expected public key
    priv.public_key().verify(signature, message)
