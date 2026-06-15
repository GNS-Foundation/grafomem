import os
import psycopg
from fastapi.testclient import TestClient
from aml.server.app import create_app, _tenant_id
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
from cryptography.fernet import Fernet

priv = Ed25519PrivateKey.generate()
raw_seed = priv.private_bytes(encoding=Encoding.Raw, format=PrivateFormat.Raw, encryption_algorithm=NoEncryption())
os.environ["ERASURE_SIGNING_KEY"] = raw_seed.hex()
os.environ["PROVIDER_ENCRYPTION_KEY"] = Fernet.generate_key().decode('utf-8')
os.environ["GRAFOMEM_CLOUD"] = "true"

db_url = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:grafomem_dev@localhost:5432/grafomem")
app = create_app(db_url=db_url, auth_mode="token", tokens={"test_fernet_tenant": "test_fernet_tenant"})
app.dependency_overrides[_tenant_id] = lambda: "test_fernet_tenant"

with TestClient(app) as client:
    resp = client.post("/v1/llm/providers", json={
        "provider": "openai",
        "model_id": "gpt-4o",
        "api_key": "sk-real-test-key-12345"
    }, headers={"Authorization": "Bearer test_fernet_tenant"})
    print("Provider registration status:", resp.status_code)
    
with psycopg.connect(db_url) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT api_key FROM llm_providers WHERE tenant_id = 'test_fernet_tenant' AND provider = 'openai' AND model_id = 'gpt-4o'")
        row = cur.fetchone()
        print(f"RAW SQL DUMP (SELECT api_key FROM llm_providers): {row[0]}")
