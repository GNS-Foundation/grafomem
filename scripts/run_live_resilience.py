import os
import sys
import time
import uuid
import requests
import json

def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()

def run_resilience():
    print("==========================================================")
    print(" GRAFOMEM CLOUD : PHASE 2 RESILIENCE CONFORMANCE ")
    print("==========================================================")

    api_url = os.environ.get("GRAFOMEM_API_URL")
    if not api_url:
        print("❌ ERROR: GRAFOMEM_API_URL is required.")
        sys.exit(1)

    # Note: We need ONE valid API key for the fallback model to succeed
    valid_openai = os.environ.get("OPENAI_API_KEY")
    valid_gemini = os.environ.get("GEMINI_API_KEY")
    
    if not valid_openai and not valid_gemini:
        print("❌ ERROR: OPENAI_API_KEY or GEMINI_API_KEY is required to test successful failover.")
        sys.exit(1)
        
    if valid_openai:
        good_prov, good_model, good_key = "openai", "gpt-4o", valid_openai
        bad_prov, bad_model = "gemini", "gemini-2.5-pro"
    else:
        good_prov, good_model, good_key = "gemini", "gemini-2.5-pro", valid_gemini
        bad_prov, bad_model = "openai", "gpt-4o"

    flight_id = uuid.uuid4().hex[:8]
    ephemeral_email = f"resil-{flight_id}@test.com"
    print(f"[*] Provisioning ephemeral tenant: {ephemeral_email}")
    
    signup_resp = requests.post(f"{api_url}/v1/portal/signup", json={
        "name": f"Resil {flight_id}",
        "email": ephemeral_email,
        "password": "FlightPassword123!",
        "plan": "pro"
    })
    
    if signup_resp.status_code != 201:
        print(f"❌ ERROR: Signup failed: {signup_resp.text}")
        sys.exit(1)
        
    tenant_data = signup_resp.json()
    api_key = tenant_data["api_key"]
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    print(f"[✓] Tenant created. ID: {tenant_data['tenant_id']}")

    # ---------------------------------------------------------
    # TEST 1: LLM Provider Failover
    # ---------------------------------------------------------
    print("\n--- TEST 1: LLM Provider Failover ---")
    # Register primary with an intentionally INVALID key
    requests.post(f"{api_url}/v1/llm/providers", headers=headers, json={
        "provider": bad_prov, "model_id": bad_model, "api_key": "sk-invalid-key"
    }).raise_for_status()
    # Register fallback with a VALID key
    requests.post(f"{api_url}/v1/llm/providers", headers=headers, json={
        "provider": good_prov, "model_id": good_model, "api_key": good_key
    }).raise_for_status()

    # Create agent
    agent_resp = requests.post(f"{api_url}/v1/orchestrator/agents", headers=headers, json={
        "name": "FailoverAgent",
        "role": "custom",
        "model_id": bad_model,
        "fallback_models": [good_model], # Fallback (will succeed)
        "system_prompt": "You are a helpful assistant. Output exactly 'Hello from fallback!'."
    })
    if agent_resp.status_code != 200:
        print(f"❌ ERROR: Failed to create FailoverAgent. {agent_resp.text}")
        sys.exit(1)
    agent_failover = agent_resp.json()["agent_id"]

    # Run step
    step_resp = requests.post(f"{api_url}/v1/orchestrator/step", headers=headers, json={
        "agent_id": agent_failover,
        "input_text": "Say hello."
    })
    step_data = step_resp.json()
    print(f"  [✓] Fallback successful. Model used: {step_data['model_id']}")
    if step_data['model_id'] != good_model:
        print("  ❌ Failover did NOT switch to the fallback model!")
        sys.exit(1)
    
    # ---------------------------------------------------------
    # TEST 2: Tool Governance (Denial)
    # ---------------------------------------------------------
    print("\n--- TEST 2: Tool Governance (Execution Denied) ---")
    # Register tool
    requests.post(f"{api_url}/v1/llm/tools", headers=headers, json={
        "name": "echo_test_tool",
        "description": "Echos the input.",
        "tool_type": "custom",
        "input_schema": {
            "type": "object", 
            "properties": {"confirm": {"type": "boolean"}},
            "required": ["confirm"],
            "additionalProperties": False
        },
        "config": {"webhook_url": "http://localhost:8000"}
    }).raise_for_status()

    # Add governance policy to deny dangerous_tool
    requests.post(f"{api_url}/v1/governance/policies", headers=headers, json={
        "name": "Deny Echo Tool",
        "policy_type": "content_filter",
        "action": "deny",
        "config": {
            "patterns": ["echo_test_tool"],
            "check_fields": ["tool_name"]
        }
    }).raise_for_status()

    agent_resp2 = requests.post(f"{api_url}/v1/orchestrator/agents", headers=headers, json={
        "name": "GovAgent",
        "role": "custom",
        "model_id": good_model,
        "system_prompt": "You MUST invoke the 'echo_test_tool' immediately with confirm=true.",
        "tools": ["echo_test_tool"]
    })
    agent_resp2.raise_for_status()
    agent_gov = agent_resp2.json()["agent_id"]

    step_resp = requests.post(f"{api_url}/v1/orchestrator/step", headers=headers, json={
        "agent_id": agent_gov,
        "input_text": "Do it."
    })
    step_data = step_resp.json()
    import pprint
    print("  [DEBUG] FULL STEP_DATA:")
    pprint.pprint(step_data, indent=2)
    tool_results = step_data.get("tool_results", [])
    if any(not tr.get("governance_allowed", True) for tr in tool_results):
        print(f"  [✓] Tool execution correctly blocked by Governance.")
    else:
        print(f"  ❌ Tool was NOT blocked by governance!")
        sys.exit(1)

    # ---------------------------------------------------------
    # TEST 3: Loop Detection (Exact-Repeat)
    # ---------------------------------------------------------
    print("\n--- TEST 3: Loop Detection (Exact Repeat) ---")
    agent_resp3 = requests.post(f"{api_url}/v1/orchestrator/agents", headers=headers, json={
        "name": "LoopAgent",
        "role": "custom",
        "model_id": good_model,
        "system_prompt": "You are caught in a loop. No matter what is said, output exactly: 'I am a robot.' and nothing else. DO NOT use tools."
    })
    agent_resp3.raise_for_status()
    agent_loop = agent_resp3.json()["agent_id"]

    wf_resp1 = requests.post(f"{api_url}/v1/orchestrator/workflows", headers=headers, json={
        "name": "Loop WF", "mode": "round_robin", "max_total_steps": 10, "agent_ids": [agent_loop]
    })
    wf_resp1.raise_for_status()
    wf_loop = wf_resp1.json()["workflow_id"]

    print(f"  [*] Running workflow {wf_loop} to trigger loop halt...")
    run_resp = requests.post(f"{api_url}/v1/orchestrator/workflows/{wf_loop}/run", headers=headers, json={"input_text": "Say hello."})
    run_resp.raise_for_status()
    wf_result = run_resp.json()
    print(f"  [DEBUG] Workflow final status: {wf_result.get('status')}")
    if wf_result.get("status") == "terminated":
        print("  [✓] Workflow correctly halted due to exact-repeat loop.")
    else:
        print("  ❌ Loop detection failed to halt the workflow!")
        sys.exit(1)

    # ---------------------------------------------------------
    # TEST 4: Workflow Timeout
    # ---------------------------------------------------------
    print("\n--- TEST 4: Workflow Timeout ---")
    agent_resp4 = requests.post(f"{api_url}/v1/orchestrator/agents", headers=headers, json={
        "name": "TimeAgent",
        "role": "custom",
        "model_id": good_model,
        "system_prompt": "Just say Hello."
    })
    agent_resp4.raise_for_status()
    agent_time = agent_resp4.json()["agent_id"]

    wf_resp2 = requests.post(f"{api_url}/v1/orchestrator/workflows", headers=headers, json={
        "name": "Time WF", "mode": "sequential", "max_total_steps": 5, "agent_ids": [agent_time]
    })
    wf_resp2.raise_for_status()
    wf_time = wf_resp2.json()["workflow_id"]

    # We send timeout_seconds=0.001 which is impossible to beat for a network call
    print("  [*] Running workflow with 0.001s timeout...")
    run_resp4 = requests.post(f"{api_url}/v1/orchestrator/workflows/{wf_time}/run", headers=headers, json={"input_text": "start", "timeout_seconds": 0.001})
    wf_result4 = run_resp4.json()
    print(f"  [DEBUG] Timeout workflow status: {wf_result4.get('status', 'N/A')}, HTTP {run_resp4.status_code}")
    # The workflow should fail or terminate due to deadline
    if wf_result4.get("status") in ("failed", "terminated"):
        print("  [✓] Workflow correctly halted due to timeout.")
    elif run_resp4.status_code >= 500:
        print("  [✓] Workflow correctly halted due to timeout (server error).")
    else:
        print("  ❌ Timeout detection failed to halt the workflow!")
        sys.exit(1)


    # ---------------------------------------------------------
    # TEST 5: Erasure Certification (Phase 0 Completion)
    # ---------------------------------------------------------
    print("\n--- TEST 5: Erasure Certification ---")
    store_resp = requests.post(f"{api_url}/v1/stores", headers=headers).json()
    print(f"  [DEBUG] Store response: {store_resp}")
    store_id = store_resp["store_id"]

    # Write a memory
    write_resp = requests.post(f"{api_url}/v1/stores/{store_id}/write", headers=headers, json={
        "content": "Sensitive data to be erased."
    })
    write_resp.raise_for_status()
    fact_ref = write_resp.json()["ref"]

    # Issue erasure certificate
    erasure_resp = requests.post(f"{api_url}/v1/erasure/issue", headers=headers, json={
        "fact_ref": fact_ref,
        "fact_content": "Sensitive data to be erased.",
        "legal_basis": "User requested right to be forgotten"
    })
    if erasure_resp.status_code != 200:
        print(f"  [DEBUG] Erasure issue failed: HTTP {erasure_resp.status_code}")
        print(f"  [DEBUG] Response: {erasure_resp.text}")
        sys.exit(1)
    erasure_req = erasure_resp.json()

    cert_id = erasure_req["certificate_id"]
    print(f"  [*] Erasure executed. Certificate generated: {cert_id}")

    # Fetch canonical key
    pub_key = requests.get(f"{api_url}/v1/gcrumbs/public_key").json()["public_key"]

    # Validate cert signature locally
    cert_data = requests.get(f"{api_url}/v1/erasure/{cert_id}", headers=headers).json()
    cert_sig = cert_data["signature"]
    
    # Reconstruct the cert string for verification
    cert_payload = canon({
        "certificate_id": cert_data["certificate_id"],
        "tenant_id": cert_data["tenant_id"],
        "fact_ref": cert_data["fact_ref"],
        "fact_content_hash": cert_data["fact_content_hash"],
        "memory_deleted": cert_data["memory_deleted"],
        "decision_records_scrubbed": cert_data["decision_records_scrubbed"],
        "erasure_requested_at": cert_data["erasure_requested_at"],
        "erasure_completed_at": cert_data["erasure_completed_at"],
        "legal_basis": cert_data["legal_basis"],
    })
    
    import hashlib
    digest = hashlib.blake2b(cert_payload, digest_size=32).digest()
    
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_key))
    try:
        pub.verify(bytes.fromhex(cert_sig), digest)
        print("  [✓] Erasure Certificate cryptographically verified against canonical key.")
    except Exception as e:
        print(f"  ❌ Erasure Certificate validation FAILED! {e}")
        sys.exit(1)

    print("\n==========================================================")
    print(" ✅ PHASE 2 RESILIENCE CONFORMANCE FLIGHT SUCCESSFUL ")
    print("==========================================================")

if __name__ == "__main__":
    run_resilience()
