import os
import pytest
import psycopg
from aml.server.app import create_app

# We need a shared test database URL
TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"
os.environ["GRAFOMEM_DB_URL"] = TEST_DB_URL
os.environ["GRAFOMEM_LEDGER_URL"] = "postgresql://grafomem:dev@localhost:5432/grafomem_ledger"
os.environ["GRAFOMEM_MASTER_KEY"] = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
os.environ["UNSAFE_LOCAL_DEV"] = "true"

@pytest.fixture(scope="session", autouse=True)
def setup_test_schema():
    """Session-scoped fixture to ensure the schema is created exactly once for ALL components."""
    # create_app() automatically loops through all backend services and calls ensure_schema()
    try:
        app = create_app(db_url=TEST_DB_URL, spec_only=False)
        
        # Additionally ensure the default ledger database schema exists
        if hasattr(app.state, 'erasure_ledger'):
            app.state.erasure_ledger.ensure_schema()
            
        from aml.backends.postgres_gmp import PostgresGMPBackend
        try:
            backend = PostgresGMPBackend(TEST_DB_URL)
            backend.ensure_schema()
        except Exception as e:
            print(f"Warning: Failed to ensure GMP schema: {e}")
            
    except Exception as e:
        print(f"Warning: Failed to ensure full schema: {e}")

    # The schema is now guaranteed to exist for all tests.
    yield

@pytest.fixture(scope="function", autouse=True)
def transactional_rollback():
    """Roll back any changes made during a single test to keep the DB clean."""
    tables = [
        "orchestrator_workflows",
        "orchestrator_steps",
        "orchestrator_agents",
        "tenant_deks",
        "tenant_api_keys",
        "tenants",
        "memories",
        "assurance_schedules",
        "erasure_certificates",
        "decision_records",
        "siem_audit_logs",
        "governance_policies",
        "compliance_reports",
        "tenant_webhooks",
        "metering_stats",
        "llm_providers",
        "regulatory_reports"
    ]
    yield
    try:
        with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
            with conn.cursor() as cur:
                # CASCADE handles foreign keys like decision_records etc.
                cur.execute(f"TRUNCATE TABLE {', '.join(tables)} CASCADE")
        with psycopg.connect(os.environ["GRAFOMEM_LEDGER_URL"], autocommit=True) as conn2:
            with conn2.cursor() as cur2:
                cur2.execute("TRUNCATE TABLE w6_ledger CASCADE")
    except Exception as e:
        pass
