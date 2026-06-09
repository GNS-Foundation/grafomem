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

import uuid
import pytest
from datetime import datetime, timezone
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from aml.cloud.erasure_proof import ErasureProofService
from aml.cloud.llm_registry import LLMRegistry, LLMProvider, LLMConfig
from aml.cloud.tool_registry import ToolRegistry, ToolType

@pytest.fixture
def temp_db_url():
    url = os.environ.get("GRAFOMEM_DB_URL")
    if not url:
        pytest.skip("GRAFOMEM_DB_URL not set")
    return url

@pytest.fixture
def erasure_service(temp_db_url):
    ep = ErasureProofService(temp_db_url)
    ep.ensure_schema()
    yield ep
    ep.close()

def test_erasure_fail_closed(erasure_service):
    """Negative test: No key -> assert crash and fact not deleted."""
    # Since we have no key, it should fail immediately.
    with pytest.raises(RuntimeError, match="Unsigned certificates are strictly prohibited"):
        erasure_service.issue_certificate("tenant1", 123)

from fastapi.testclient import TestClient
from aml.server.app import create_app

def test_rest_erasure_fail_closed(temp_db_url, monkeypatch):
    """Negative test: REST DELETE without key -> crash and fact not deleted."""
    # We must ensure PROVIDER_ENCRYPTION_KEY is set or create_app will crash (because db_url is set).
    # conftest.py already sets PROVIDER_ENCRYPTION_KEY.
    
    # 1. Create app without an ERASURE_SIGNING_KEY in environment
    monkeypatch.delenv("ERASURE_SIGNING_KEY", raising=False)
        
    def _test_factory():
        from aml.backends.postgres_gmp import PostgresGMPBackend
        return PostgresGMPBackend(temp_db_url)

    app = create_app(
        backend_factory=_test_factory,
        db_url=temp_db_url,
        auth_mode="token",
        tokens={"test-token": "tenant1"}
    )
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer test-token"})
    
    # 2. Write a fact
    resp = client.post("/v1/stores", json={})
    assert resp.status_code == 200
    store_id = resp.json()["store_id"]
    
    w_resp = client.post(f"/v1/stores/{store_id}/write", json={"content": "super secret REST fact"})
    assert w_resp.status_code == 200
    ref = w_resp.json()["ref"]
    
    # 3. Attempt delete
    d_resp = client.post(f"/v1/stores/{store_id}/delete", json={"ref": ref})
    assert d_resp.status_code == 503
    assert "strictly prohibited" in d_resp.json()["detail"] or "unavailable" in d_resp.json()["detail"]
    
    # 4. Verify fact is STILL PRESENT (fail-closed)
    r_resp = client.post(f"/v1/stores/{store_id}/retrieve", json={"query": "super secret"})
    mems = r_resp.json().get("memories", [])
    assert len(mems) > 0
    assert mems[0]["content"] == "super secret REST fact"

def test_erasure_positive():
    """Positive test: Key set -> cert issued and verifies."""
    db_url = os.environ.get("GRAFOMEM_DB_URL")
    if not db_url:
        pytest.skip("GRAFOMEM_DB_URL not set")
    # Generate Ed25519 seed
    private_key = Ed25519PrivateKey.generate()
    seed = private_key.private_bytes_raw()
    
    class _MockId:
        def sign(self, m): priv=Ed25519PrivateKey.from_private_bytes(seed); return priv.sign(m), priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        def public_key(self): return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ep = ErasureProofService(db_url, signing_identity=_MockId())
    ep.ensure_schema()
    
    cert = ep.issue_certificate("tenant1", 123)
    assert cert.certificate_id is not None
    assert cert.signature is not None
    assert cert.public_key is not None
    
    # Verify the signature
    verification = ep.verify_certificate(cert.certificate_id)
    assert verification["valid"] is True, verification.get("detail", "Unknown")
    
    ep.close()

def test_multi_fernet_rotation():
    """Verify MultiFernet encrypts with new key but decrypts legacy."""
    db_url = os.environ.get("GRAFOMEM_DB_URL")
    if not db_url:
        pytest.skip("GRAFOMEM_DB_URL not set")
    
    key_a = Fernet.generate_key().decode()
    key_b = Fernet.generate_key().decode()
    
    # Environment with only Key A
    os.environ["PROVIDER_ENCRYPTION_KEY"] = key_a
    from aml.cloud.identity import EnvIdentity
    reg1 = LLMRegistry(db_url, encryption=EnvIdentity())
    reg1.ensure_schema()
    
    c1 = reg1.register_provider("tenant_a", LLMProvider.OPENAI, "gpt-4", api_key="secret-123")
    
    # Verify it was encrypted in the DB
    conn = reg1._get_conn()
    raw_row = conn.execute("SELECT api_key FROM llm_providers WHERE config_id = %s", (c1.config_id,)).fetchone()
    assert raw_row["api_key"] != "secret-123"
    assert raw_row["api_key"].startswith("gAAAAA")
    
    # Check in-memory property
    c1_read = reg1.get_provider("tenant_a", "gpt-4")
    assert c1_read.api_key == "secret-123"
    
    # Now rotate keys: Key B is primary, Key A is secondary
    os.environ["PROVIDER_ENCRYPTION_KEY"] = f"{key_b},{key_a}"
    reg2 = LLMRegistry(db_url, encryption=EnvIdentity())
    
    # Can still read old key
    c1_read_rotated = reg2.get_provider("tenant_a", "gpt-4")
    assert c1_read_rotated.api_key == "secret-123"
    
    # Write a new key, should use Key B
    c2 = reg2.register_provider("tenant_a", LLMProvider.ANTHROPIC, "claude-3", api_key="secret-456")
    raw_row2 = conn.execute("SELECT api_key FROM llm_providers WHERE config_id = %s", (c2.config_id,)).fetchone()
    
    # We can't easily assert which key was used just by looking at the string, 
    # but we can ensure it decrypts with reg2
    c2_read = reg2.get_provider("tenant_a", "claude-3")
    assert c2_read.api_key == "secret-456"
    
    # And reg1 (only knows A) should fail to decrypt c2 (encrypted with B)
    with pytest.raises(ValueError, match="Decryption failures are strictly denied"):
        reg1.get_provider("tenant_a", "claude-3")

    reg1.close()
    reg1.close()
    reg2.close()

def test_strict_decryption_error():
    """Verify that corrupt/plaintext ciphertext throws a clean error."""
    db_url = os.environ.get("GRAFOMEM_DB_URL")
    if not db_url:
        pytest.skip("GRAFOMEM_DB_URL not set")
    key_a = Fernet.generate_key().decode()
    os.environ["PROVIDER_ENCRYPTION_KEY"] = key_a
    
    from aml.cloud.identity import EnvIdentity
    reg = LLMRegistry(db_url, encryption=EnvIdentity())
    reg.ensure_schema()
    
    # Insert corrupt data directly
    c_id = uuid.uuid4().hex[:24]
    tenant = f"t_err_{c_id}"
    conn = reg._get_conn()
    conn.execute(
        "INSERT INTO llm_providers "
        "(config_id, tenant_id, provider, model_id, api_key) "
        "VALUES (%s, %s, %s, %s, %s)",
        (c_id, tenant, "openai", "gpt-err", "legacy_unencrypted_key_or_garbage")
    )
    
    with pytest.raises(ValueError, match="Decryption failures are strictly denied"):
        reg.get_provider(tenant, "gpt-err")

    reg.close()

def test_encryption_required_fail_closed(monkeypatch):
    """Verify EnvIdentity fails closed if encryption key missing and not opted out."""
    monkeypatch.delenv("PROVIDER_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("UNSAFE_LOCAL_DEV", raising=False)
        
    from aml.cloud.identity import EnvIdentity
    with pytest.raises(ValueError, match="PROVIDER_ENCRYPTION_KEY is required"):
        EnvIdentity()
