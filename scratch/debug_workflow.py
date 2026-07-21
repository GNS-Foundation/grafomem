import httpx
import uuid
import time
url = "https://grafomem-staging-staging.up.railway.app"
tid = str(uuid.uuid4())
r = httpx.post(f"{url}/v1/portal/signup", json={"tenant_id": tid, "name": "Test", "email": f"test_{tid}@test.com", "password": "Password123!"})
api_key = r.json().get("api_key")
headers = {"Authorization": f"Bearer {api_key}"}

agent = httpx.post(f"{url}/v1/orchestrator/agents", headers=headers, json={"name": "test", "model_id": "mock-model", "system_prompt": "You are agent.", "role": "test", "description": "test", "fallback_models": [], "memory_stores": [], "max_steps": 5, "max_tokens_per_step": 1000, "temperature": 0.7})
agent_id = agent.json()["agent_id"]

wf = httpx.post(f"{url}/v1/orchestrator/workflows", headers=headers, json={"name": "Test WF", "description": "test", "agent_ids": [agent_id], "mode": "sequential", "max_total_steps": 10})
wf_id = wf.json().get("workflow_id")

step = httpx.post(f"{url}/v1/orchestrator/workflows/{wf_id}/run", headers=headers, json={"input_text": "run test"})
print("Workflow run:", step.json())
