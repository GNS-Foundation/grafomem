import os
import sys
import uuid
import requests

def main():
    api_url = os.environ.get("GRAFOMEM_API_URL")
    if not api_url:
        print("❌ ERROR: GRAFOMEM_API_URL is required.")
        sys.exit(1)

    flight_id = uuid.uuid4().hex[:8]
    ephemeral_email = f"resil-f1-{flight_id}@test.com"

    signup_resp = requests.post(f"{api_url}/v1/portal/signup", json={
        "name": f"Resil Finding 1",
        "email": ephemeral_email,
        "password": "FlightPassword123!",
        "plan": "pro"
    })
    signup_resp.raise_for_status()
    tenant_data = signup_resp.json()
    api_key = tenant_data["api_key"]
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    
    # Register bad provider with empty key (simulating missing key)
    print("\n[+] Registering provider with EMPTY api_key")
    requests.post(f"{api_url}/v1/llm/providers", headers=headers, json={
        "provider": "openai", "model_id": "gpt-4o", "api_key": ""
    }).raise_for_status()
    
    agent_resp = requests.post(f"{api_url}/v1/orchestrator/agents", headers=headers, json={
        "name": "F1_Agent", "role": "custom", "model_id": "gpt-4o",
        "system_prompt": "Output exactly: 'Hello'"
    })
    agent_resp.raise_for_status()
    agent_id = agent_resp.json()["agent_id"]

    print("[+] Executing step (should fail and not fall back to platform key)")
    step_resp = requests.post(f"{api_url}/v1/orchestrator/step", headers=headers, json={
        "agent_id": agent_id,
        "input_text": "Say hello."
    })
    step_data = step_resp.json()
    print("Step Status:", step_data.get("status"))
    print("Raw Output:", step_data.get("raw_output"))

    # Register bad provider with invalid key (sk-invalid-key)
    print("\n[+] Registering provider with INVALID api_key")
    requests.post(f"{api_url}/v1/llm/providers", headers=headers, json={
        "provider": "openai", "model_id": "gpt-4o", "api_key": "sk-invalid-key"
    }).raise_for_status()
    
    print("[+] Executing step (should fail with 401 Unauthorized)")
    step_resp2 = requests.post(f"{api_url}/v1/orchestrator/step", headers=headers, json={
        "agent_id": agent_id,
        "input_text": "Say hello."
    })
    step_data2 = step_resp2.json()
    print("Step Status:", step_data2.get("status"))
    print("Raw Output:", step_data2.get("raw_output"))

if __name__ == "__main__":
    main()
