import os
import uuid
import psycopg
from psycopg.rows import dict_row
from aml.server.app import create_app
from fastapi.testclient import TestClient
from aml.cloud.tenant_manager import TenantManager

os.environ["GRAFOMEM_DB_URL"] = "postgresql://grafomem:dev@localhost:5432/grafomem"
os.environ["AUTH_MODE"] = "cloud"

tm = TenantManager(os.environ["GRAFOMEM_DB_URL"])
info = tm.create_tenant(name=f"test-{uuid.uuid4().hex[:8]}")
agent_key = tm.create_api_key(info.id, name="agent_key", role="agent")

conn = psycopg.connect(os.environ["GRAFOMEM_DB_URL"], row_factory=dict_row)
print("tenant_api_keys:", conn.execute("SELECT api_key, role FROM tenant_api_keys WHERE api_key = %s", (agent_key,)).fetchone())

app = create_app()
client = TestClient(app)

response = client.get("/v1/cloud/tenants", headers={"Authorization": f"Bearer {agent_key}"})
print("Cloud Get:", response.status_code)
