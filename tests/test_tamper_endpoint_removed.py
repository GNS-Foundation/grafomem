"""B2: the sandboxed tamper endpoint is gone. With ENABLE_TAMPER_ENDPOINT=1 the app must not mount
/v1/_system/run_tamper_proof. MUST-FAIL on main 1964684: there the route is mounted and an
authenticated tenant key gets 403 from the admin:platform gate, not 404. The handler raises 403 at
its first statement, before any DML; the only writes in the red run are the fixture's tenant and
key, which conftest's transactional_rollback truncates."""
import os, uuid
import pytest
from fastapi.testclient import TestClient
from aml.cloud.tenant_manager import TenantManager
from aml.server.app import create_app
from aml.server.scopes import TENANT_ADMIN_SCOPES

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
PATH = "/v1/_system/run_tamper_proof"

@pytest.fixture(scope="module")
def app_with_flag():
    os.environ["ENABLE_TAMPER_ENDPOINT"] = "1"
    os.environ["GRAFOMEM_DB_URL"] = DB_URL
    os.environ["GRAFOMEM_AUTH_MODE"] = "cloud"
    os.environ.setdefault("GRAFOMEM_SIGNING_KEY", "b" * 64)
    try:
        yield create_app(db_url=DB_URL)
    finally:
        os.environ.pop("ENABLE_TAMPER_ENDPOINT", None)

@pytest.fixture  # function scope: conftest truncates tenants/tenant_api_keys after every test
def admin_key():
    tm = TenantManager(DB_URL); tm.ensure_schema()
    t = tm.create_tenant(name=f"b2-{uuid.uuid4().hex[:8]}")
    return tm.create_api_key(t.id, name="b2", role="admin", scopes=TENANT_ADMIN_SCOPES)["api_key"]

def test_no_system_routes_are_mounted(app_with_flag):
    paths = {getattr(r, "path", "") for r in app_with_flag.routes}
    assert not any(p.startswith("/v1/_system") for p in paths), sorted(p for p in paths if "_system" in p)

def test_post_run_tamper_proof_is_404(app_with_flag, admin_key):
    with TestClient(app_with_flag) as c:
        assert c.get("/health").status_code == 200            # positive control: the app built and serves
        r = c.post(PATH, headers={"X-API-Key": admin_key})
        assert r.status_code == 404, f"{r.status_code} {r.text}"
