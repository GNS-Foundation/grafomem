import os
import requests
import random

BASE_URL = "https://grafomem-production.up.railway.app"
nonce = random.randint(1000, 9999)

r_signup = requests.post(f"{BASE_URL}/v1/portal/signup", json={"name": f"Tamper Test {nonce}", "email": f"tamper.{nonce}@example.com", "password": "password123"})
if r_signup.status_code not in (200, 201):
    print("Signup failed:", r_signup.text)
    exit(1)

jwt_token = r_signup.json()["token"]

r = requests.post(
    f"{BASE_URL}/v1/_system/run_tamper_proof",
    headers={"Authorization": f"Bearer {jwt_token}"}
)
print("Status:", r.status_code)
print("Response:", r.text)
