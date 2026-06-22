import requests
import json
print(requests.post("https://grafomem-production.up.railway.app/v1/portal/api-keys", json={"name": "Expired Key", "expires_at": 100}, headers={"Authorization": "Bearer fake"}).json())
