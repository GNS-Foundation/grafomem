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

def create_all_test_schema():
    """Create ALL schema on a fresh DB by instantiating the schema-owning services
    DIRECTLY (not via create_app's app.state).

    Why not create_app(): on a fresh DB, create_app constructs services that query
    tables at __init__ (e.g. manifold does `SELECT ... FROM orchestrator_steps`),
    which errors before those tables exist and can leave app.state.<svc> = None —
    so relying on app.state to reach ensure_schema() fails silently, and the very
    tables the tests need never get created (green locally where a reused DB kept
    them, red on fresh CI). Direct instantiation breaks that chicken-and-egg;
    orchestrator schema is created FIRST so any later create_app (the test modules'
    own app fixtures) finds orchestrator_steps and initializes cleanly.
    """
    DB = TEST_DB_URL
    LEDGER = os.environ["GRAFOMEM_LEDGER_URL"]
    MK = os.environ["GRAFOMEM_MASTER_KEY"]

    def _ensure(label, make):
        # Close each service's connection after creating its schema — these services
        # hold a lazy connection open, and ~20 of them would exhaust CI's
        # max_connections (100), starving the test apps' create_app of connections.
        svc = None
        try:
            svc = make()
            svc.ensure_schema()
        except Exception as e:
            print(f"Warning: ensure {label} schema failed: {e}")
        finally:
            if svc is not None and hasattr(svc, "close"):
                try:
                    svc.close()
                except Exception:
                    pass

    from aml.backends.postgres_gmp import PostgresGMPBackend
    from aml.cloud.tenant_manager import TenantManager
    from aml.cloud.tenant_key_manager import TenantKeyManager
    from aml.cloud.decision_trail import DecisionTrailService
    from aml.cloud.execution_receipts import ExecutionReceiptService
    from aml.cloud.gcrumbs import GcrumbsService
    from aml.cloud.governance import GovernanceGateway
    from aml.cloud.orchestrator import OrchestratorService
    from aml.cloud.erasure_ledger import ErasureLedger
    from aml.cloud.assurance import AssuranceService
    from aml.cloud.compliance import ComplianceTracker
    from aml.cloud.evidence_collector import EvidenceCollector
    from aml.cloud.llm_registry import LLMRegistry
    from aml.cloud.metering import MeteringService
    from aml.cloud.memory_taxonomy import WorkflowContextService
    from aml.cloud.webhook_service import WebhookService
    from aml.cloud.landing_service import LandingService
    from aml.cloud.world_model import WorldModelService
    from aml.cloud.artifact_registry import ArtifactRegistryService
    from aml.cloud.erasure_proof import ErasureProofService

    # orchestrator FIRST — creates orchestrator_steps that manifold queries at init.
    _tkm = TenantKeyManager(MK, DB)                                    # also the encryption identity
    _ensure("orchestrator", lambda: OrchestratorService(DB, None, None))
    _ensure("tenant", lambda: TenantManager(DB))                       # tenants, tenant_api_keys
    _ensure("tenant_dek", lambda: _tkm)                               # tenant_deks
    _ensure("decision_trail", lambda: DecisionTrailService(DB))
    _ensure("execution_receipts", lambda: ExecutionReceiptService(DB))
    _ensure("gcrumbs", lambda: GcrumbsService(DB))                     # gcrumbs_epochs/breadcrumbs
    _ensure("governance", lambda: GovernanceGateway(DB))              # governance_policies
    _ensure("assurance", lambda: AssuranceService(DB))               # assurance_schedules
    _ensure("compliance", lambda: ComplianceTracker(DB))
    _ensure("evidence", lambda: EvidenceCollector(DB))
    _ensure("llm_registry", lambda: LLMRegistry(DB, encryption=_tkm))  # llm_providers
    _ensure("metering", lambda: MeteringService(DB))
    _ensure("workflow_context", lambda: WorkflowContextService(DB))
    _ensure("webhook", lambda: WebhookService(DB))
    _ensure("landing", lambda: LandingService(DB))
    _ensure("world_model", lambda: WorldModelService(DB))
    _ensure("artifact_registry", lambda: ArtifactRegistryService(DB))
    _ensure("erasure_proof", lambda: ErasureProofService(DB))        # erasure_certificates
    try:
        _gmp = PostgresGMPBackend(DB)                                 # memories: schema created in __init__
        if hasattr(_gmp, "close"):
            _gmp.close()
    except Exception as e:
        print(f"Warning: GMP construct failed: {e}")
    _ensure("erasure_ledger", lambda: ErasureLedger(LEDGER))          # ledger DB
    if hasattr(_tkm, "close"):
        try:
            _tkm.close()
        except Exception:
            pass

    # No create_app supplement — it opens another pool (more connections) and its
    # app.state services are None on a fresh DB anyway. The direct instantiations
    # above create every schema the tests need; the test modules' own create_app
    # (with connections now free) finds the tables and mounts all routers.

    # Migrations LAST — they ALTER the base tables created above (e.g. add
    # orchestrator_agents.system_prompt_enc, tenant_api_keys.scopes).
    try:
        from aml.cloud.migrations_runner import apply_migrations
        apply_migrations(DB)
    except Exception as e:
        print(f"Warning: apply_migrations failed: {e}")


@pytest.fixture(scope="session", autouse=True)
def setup_test_schema():
    create_all_test_schema()
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
