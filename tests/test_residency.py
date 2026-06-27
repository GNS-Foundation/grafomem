import os
import pytest
import psycopg
from datetime import datetime, timezone
from aml.backends.postgres_gmp import PostgresGMPBackend
from aml.backends.interface import WriteOptions, RetrieveOptions
from aml.cloud.tenant_manager import TenantManager

@pytest.fixture
def temp_db_url():
    url = os.environ.get("GRAFOMEM_DB_URL")
    if not url:
        pytest.skip("GRAFOMEM_DB_URL not set")
    return url

def test_data_residency_enforcement(temp_db_url):
    """
    Ensures that geographic boundaries established by region tags
    are strictly enforced at the database query level during retrieval.
    """
    # 1. Clean environment
    backend = PostgresGMPBackend(temp_db_url)
    
    with psycopg.connect(temp_db_url) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memory_embeddings")
            cur.execute("DELETE FROM memories")
            cur.execute("DELETE FROM tenants")
            conn.commit()
            
    # 2. Test TenantManager home_region assignment
    tm = TenantManager(temp_db_url)
    tm.ensure_schema()
    tenant_eu = tm.create_tenant("EU Tenant", home_region="eu-central-1")
    tenant_us = tm.create_tenant("US Tenant", home_region="us-east-1")
    
    assert tenant_eu.home_region == "eu-central-1"
    assert tenant_us.home_region == "us-east-1"
    
    # 3. Write memories with specific regions
    # EU Region memory
    backend.write_many([
        ("European customer data: Jane Doe lives in Paris.", WriteOptions(tenant_id=tenant_eu.id, region="eu-central-1")),
        ("Another EU record.", WriteOptions(tenant_id=tenant_eu.id, region="eu-central-1")),
    ])
    
    # US Region memory
    backend.write_many([
        ("US customer data: John Smith lives in NY.", WriteOptions(tenant_id=tenant_us.id, region="us-east-1")),
    ])
    
    # We will write a cross-region memory for the US tenant (to simulate an intentional shared region, or an attack)
    backend.write_many([
        ("Leaked EU data inside US tenant.", WriteOptions(tenant_id=tenant_us.id, region="eu-central-1")),
    ])
    
    # 4. Enforce strict geographic boundaries on search
    
    # The US tenant requests data, but we strictly pin their search to their home region (us-east-1)
    opts_us = RetrieveOptions(tenant_id=tenant_us.id, region="us-east-1", budget_tokens=100)
    res_us = backend.retrieve("customer data", opts_us)
    
    # Should ONLY get the US record, NOT the leaked EU data that they own, because we forced `region="us-east-1"`
    assert len(res_us) == 1
    assert "John Smith" in res_us[0].content
    assert res_us[0].region == "us-east-1"
    
    # The EU tenant requests data, pinned to eu-central-1
    opts_eu = RetrieveOptions(tenant_id=tenant_eu.id, region="eu-central-1", budget_tokens=100)
    res_eu = backend.retrieve("customer data", opts_eu)
    
    # Should get their EU records
    assert len(res_eu) == 2
    for r in res_eu:
        assert r.region == "eu-central-1"

    # Test audit output contains region
    audit_logs = list(backend.audit())
    assert len(audit_logs) == 4
    for log in audit_logs:
        assert log.region in ["eu-central-1", "us-east-1"]
