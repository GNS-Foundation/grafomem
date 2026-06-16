import os
import uuid
from aml.server.app import create_app
from fastapi.testclient import TestClient
from aml.cloud.tenant_manager import TenantManager

os.environ["GRAFOMEM_DB_URL"] = "postgresql://grafomem:dev@localhost:5432/grafomem"
os.environ["AUTH_MODE"] = "cloud"

tm = TenantManager(os.environ["GRAFOMEM_DB_URL"])
tm.ensure_schema()
info = tm.create_tenant(name=f"test-{uuid.uuid4().hex[:8]}")
tenant_id = info.id
admin_key = info.api_key
agent_key = tm.create_api_key(tenant_id, name="agent_key", role="agent")

app = create_app()
client = TestClient(app)

print("Admin key:", admin_key)
print("Agent key:", agent_key)

response = client.post("/v1/stores", headers={"Authorization": f"Bearer {agent_key}"})
print("Agent Create Store:", response.status_code, response.text)
