import os
import sys
import httpx

api_key = os.environ.get("ANTHROPIC_API_KEY")
if not api_key:
    print("blocked — no ANTHROPIC_API_KEY")
    sys.exit(1)

# We want to run against STAGING: grafomem-staging-staging.up.railway.app
# But wait, how do we make an Anthropic request? We make a request to our STAGING backend,
# providing the anthropic API key to the backend, OR configuring the backend to use it.
# Actually, the e2e test uses `X-Anthropic-Api-Key` header or `Authorization: Bearer <tenant_api_key>`.
# Wait! In the mock e2e test, we register a provider using `POST /api/v1/providers`.
# Let's register Anthropic for a test tenant, then do two identical requests.

STAGING_URL = "https://grafomem-staging-staging.up.railway.app"

client = httpx.Client(base_url=STAGING_URL)

tenant_id = "tenant_test_cache"

# 1. Register Provider
r = client.post(f"/api/v1/tenants/{tenant_id}/providers", json={
    "provider": "anthropic",
    "model_id": "claude-3-haiku-20240307",
    "api_key": api_key,
    "enabled": True
})

if r.status_code not in (200, 201):
    print("Failed to register provider:", r.text)
    sys.exit(1)

# 2. Ingest some memory to make the prompt large enough to cache
text = "This is a long piece of memory " * 50
r = client.post(f"/api/v1/tenants/{tenant_id}/memories", json={
    "content": text,
    "metadata_": {"source": "test"}
})

# 3. Make the first query
q = {"query": "What is the memory?", "stream": False, "temperature": 0.0}
print("Call 1:")
r1 = client.post(f"/api/v1/tenants/{tenant_id}/search", json=q)
if r1.status_code == 200:
    data1 = r1.json()
    print(data1.get("decision", {}).get("telemetry", {}))
else:
    print("Failed call 1:", r1.text)

print("\nCall 2:")
r2 = client.post(f"/api/v1/tenants/{tenant_id}/search", json=q)
if r2.status_code == 200:
    data2 = r2.json()
    print(data2.get("decision", {}).get("telemetry", {}))
    telemetry = data2.get("decision", {}).get("telemetry", {})
    t_read = telemetry.get("tokens_cached_read", 0)
    t_input = telemetry.get("tokens_input", 1)
    print(f"MEASURED cache savings: {t_read / (t_read + t_input) * 100:.1f}%")
else:
    print("Failed call 2:", r2.text)
