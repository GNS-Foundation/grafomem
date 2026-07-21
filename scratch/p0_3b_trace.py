import requests

API_URL = "https://grafomem-production.up.railway.app"
MASTER_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

r = requests.post(f"{API_URL}/v1/tenants/admin/setup", json={"name": "test_tenant", "plan": "enterprise"}, headers={"Authorization": f"Bearer {MASTER_KEY}"})
tenant = r.json()
print("Tenant:", tenant)

r = requests.post(f"{API_URL}/v1/stores", headers={"Authorization": f"Bearer {tenant['api_key']}"})
store_id = r.json().get("store_id")
print("Store:", store_id)

r = requests.post(f"{API_URL}/v1/stores/{store_id}/retrieve", json={"query": "test", "limit": 5}, headers={"Authorization": f"Bearer {tenant['api_key']}"})
print("Retrieve Status:", r.status_code)
print("Retrieve Body:", r.text)
