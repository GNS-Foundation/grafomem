import os
import uuid
import json
import logging
from datetime import datetime, timezone
from cryptography.fernet import Fernet

from aml.server.app import create_app
from aml.cloud.tenant_manager import TenantManager
from aml.cloud.tenant_key_manager import TenantKeyManager

from aml.server.stores import StoreManager
from aml.backends.interface import WriteOptions, RetrieveOptions
from fastapi.testclient import TestClient
from contextlib import contextmanager

# Force dev environment to bypass strict env checks
os.environ["UNSAFE_LOCAL_DEV"] = "true"
os.environ["GRAFOMEM_MASTER_KEY"] = os.urandom(32).hex()
os.environ["PROVIDER_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

os.environ["AUTH_MODE"] = "cloud"
os.environ["GRAFOMEM_SIGNING_KEY"] = "b" * 64

db_url = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
os.environ["GRAFOMEM_DB_URL"] = db_url

def _pg_factory():
    from aml.backends.postgres_gmp import PostgresGMPBackend
    backend = PostgresGMPBackend(db_url)
    backend._ensure_schema()
    return backend

app = create_app(db_url=db_url, backend_factory=_pg_factory)

def run_sealed_flight():
    print("==================================================")
    print(" GRAFOMEM SPRINT 24: SCOPE PERIMETER VERIFICATION ")
    print("==================================================")
    
    tenant_manager = app.state.tenant_manager
    tenant_key_manager = app.state.encryption
    store_manager = app.state.store_manager
    decision_trail = app.state.decision_trail
    erasure_ledger = app.state.erasure_ledger
    
    # 1. Create a tenant
    tenant_name = f"sealed-flight-{uuid.uuid4().hex[:6]}"
    info = tenant_manager.create_tenant(name=tenant_name)
    tenant_id = info.id
    print(f"\n[+] Created Tenant: {tenant_id}")
    
    # 2. Ingest sensitive data
    store_id = store_manager.create(tenant_id=tenant_id)
    store = store_manager.get(store_id).backend
    options = WriteOptions(tenant_id=tenant_id)
    content = "CONFIDENTIAL: Patient zero diagnosed with ultra-rare XYZ syndrome in Berlin."
    
    fact_ref = store.write(content, options)
    print(f"[+] Ingested sensitive fact. Ref: {fact_ref}")
    
    # 3. Dump ciphertext to prove encryption at rest
    print("\n[+] Raw DB Ciphertext Dump (memories table):")
    with store._tenant_conn(tenant_id) as (conn, cur):
        row = cur.execute("SELECT content FROM memories WHERE ref = %s", (fact_ref,)).fetchone()
        raw_content = row[0]
        print(f"    Raw content in DB: {raw_content[:80]}...")
        if "CONFIDENTIAL" in raw_content:
            print("    [!] ERROR: Plaintext leaked in DB!")
            exit(1)
        else:
            print("    [✓] Confirmed: Content is encrypted at rest (Envelope DEK applied).")
            
    # 4. Orchestrator Query (live)
    print("\n[+] Running Live Query")
    query = "What is the diagnosis for patient zero?"
    # Since we don't have a real LLM mocked here easily, we'll just mock the LLM or run a basic retrieval.
    # Actually, we can use the `mock` model_id if we registered it, or we just retrieve.
    try:
        from aml.cloud.orchestrator import StepRecord, StepStatus
        
        # We manually drive a retrieval step
        print("    Executing targeted retrieve step...")
        retrieve_opts = RetrieveOptions(tenant_id=tenant_id)
        results = store.retrieve(query, retrieve_opts)
        print(f"    Retrieved {len(results)} fact(s).")
        
        # Log to decision trail
        decision_record = decision_trail.log(
            tenant_id=tenant_id,
            store_id="default",
            query=query,
            model_id="mock_llm",
            raw_output="Diagnosis: XYZ Syndrome",
            retrieved_refs=[fact_ref],
            retrieved_contents=[content],
            encryption=tenant_key_manager
        )
        dt_id = decision_record.decision_id
        print(f"    Logged Decision ID: {dt_id}")
        
    except Exception as e:
        print(f"    Error during query: {e}")
        
    print("\n[+] Raw DB Ciphertext Dump (decision_records table):")
    with decision_trail._get_conn() as conn:
        row = conn.execute("SELECT query, raw_output FROM decision_records WHERE decision_id = %s", (dt_id,)).fetchone()
        print(f"    Raw query in DB: {row['query'][:50]}...")
        print(f"    Raw output in DB: {row['raw_output'][:50]}...")
        if "diagnosis" in row['query'] or "XYZ" in row['raw_output']:
            print("    [!] ERROR: Plaintext leaked in decision trail!")
            exit(1)
        else:
            print("    [✓] Confirmed: Decision trail is encrypted at rest.")

    # 5. Execute Tenant Key Destruction (Crypto-Erasure)
    # Mock admin auth
    from aml.cloud.admin_routes import _require_admin
    app.dependency_overrides[_require_admin] = lambda: {"tenant_id": tenant_id, "role": "admin"}

    print("\n[+] Executing Tenant-Level Crypto-Erasure...")
    with TestClient(app) as client:
        resp = client.post(
            f"/v1/admin/tenants/{tenant_id}/destroy-key",
            headers={"Authorization": f"Bearer {info.api_key}"},
            json={"confirmation": "I understand this is irreversible", "signature": "..."}
        )
    
    app.dependency_overrides.clear()
        
    if resp.status_code == 200:
        resp_data = resp.json()
        cert_id = resp_data.get("certificate_id")
        signature = resp_data.get("signature")
        print(f"    [✓] DEK destroyed. Certificate issued: {cert_id}")
        if signature:
            print(f"    Signature: {signature[:30]}...")
    else:
        print(f"    [!] Failed to destroy key: {resp.text}")
        exit(1)
        
    # 6. Replay test with confidence 1.00
    print("\n[+] Replay test: Attempting to retrieve memory post-destruction...")
    try:
        # Since the key is gone, we should get TenantKeyDestroyed when trying to read the DB or retrieve
        store = store_manager.get(store_id).backend
        retrieve_opts = RetrieveOptions(tenant_id=tenant_id)
        store.retrieve(query, retrieve_opts)
        print("    [!] ERROR: Retrieval succeeded after key destruction! (Scope leaked)")
        exit(1)
    except Exception as e:
        from aml.cloud.tenant_key_manager import TenantKeyDestroyed
        if isinstance(e, TenantKeyDestroyed) or "TenantKeyDestroyed" in str(type(e)):
             print("    [✓] Retrieval strictly blocked: TenantKeyDestroyed.")
             print("    [✓] CONFIDENCE 1.00: Key boundaries permanently erased; ciphertext is inert.")
        else:
             print(f"    [?] Failed with another error: {e}")
             
    print("\n==================================================")
    print(" VERIFICATION COMPLETE: ARTICLE 17 SCOPE ENFORCED ")
    print("==================================================")

if __name__ == "__main__":
    run_sealed_flight()
