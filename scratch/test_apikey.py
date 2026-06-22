import os
import requests
import time
import random

BASE_URL = "https://grafomem-production.up.railway.app"
nonce = random.randint(1000, 9999)

r_signup = requests.post(f"{BASE_URL}/v1/portal/signup", json={"name": f"Perimeter Test {nonce}", "email": f"peri.local.{nonce}@example.com", "password": "password123"})
if r_signup.status_code not in (200, 201):
    print("Signup failed:", r_signup.text)
    exit(1)

data = r_signup.json()
jwt_token = data["token"]

h = {"Authorization": f"Bearer {jwt_token}"}

future = int(time.time()) + 3600
past = int(time.time()) - 3600

r_key_exp = requests.post(f"{BASE_URL}/v1/portal/api-keys", json={"name": "Expired Key", "expires_at": past}, headers=h)
if r_key_exp.status_code in (200, 201):
    exp_key = r_key_exp.json()["api_key"]
    r_check = requests.get(f"{BASE_URL}/v1/governance/policies", headers={"Authorization": f"Bearer {exp_key}"})
    print("Expired key status:", r_check.status_code, "(Expect 401)")
else:
    print("Could not create expired key:", r_key_exp.text)

r_key_ip = requests.post(f"{BASE_URL}/v1/portal/api-keys", json={"name": "IP Key", "ip_allowlist": ["192.168.1.1"]}, headers=h)
if r_key_ip.status_code in (200, 201):
    ip_key = r_key_ip.json()["api_key"]
    r_check = requests.get(f"{BASE_URL}/v1/governance/policies", headers={"Authorization": f"Bearer {ip_key}"})
    print("IP allowlist status:", r_check.status_code, "(Expect 401 or 403)")
else:
    print("Could not create IP key:", r_key_ip.text)
