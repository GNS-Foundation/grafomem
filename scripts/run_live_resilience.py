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
    valid_gemini_key = os.environ.get("GEMINI_API_KEY")
    if not valid_gemini_key:
        print("❌ ERROR: GEMINI_API_KEY is required to test successful failover.")
        sys.exit(1)

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
        "provider": "openai", "model_id": "gpt-4o", "api_key": "sk-invalid-key"
    }).raise_for_status()
    # Register fallback with a VALID key
    requests.post(f"{api_url}/v1/llm/providers", headers=headers, json={
        "provider": "gemini", "model_id": "gemini-2.5-pro", "api_key": valid_gemini_key
    }).raise_for_status()

    # Create agent
    agent_failover = requests.post(f"{api_url}/v1/orchestrator/agents", headers=headers, json={
        "name": "FailoverAgent",
        "role": "worker",
        "model_id": "gpt-4o",  # Primary (will fail)
        "fallback_models": ["gemini-2.5-pro"], # Fallback (will succeed)
        "system_prompt": "You are a helpful assistant. Output exactly 'Hello from fallback!'."
    }).json()["agent_id"]

    # Run step
    step_resp = requests.post(f"{api_url}/v1/orchestrator/step", headers=headers, json={
        "agent_id": agent_failover,
        "input_text": "Say hello."
    })
    step_data = step_resp.json()
    print(f"  [✓] Fallback successful. Model used: {step_data['model_id']}")
    if step_data['model_id'] != "gemini-2.5-pro":
        print("  ❌ Failover did NOT switch to the fallback model!")
        sys.exit(1)
    
    # ---------------------------------------------------------
    # TEST 2: Tool Governance (Denial)
    # ---------------------------------------------------------
    print("\n--- TEST 2: Tool Governance (Execution Denied) ---")
    # Register tool
    requests.post(f"{api_url}/v1/tools/register", headers=headers, json={
        "name": "dangerous_tool",
        "description": "Deletes the internet.",
        "input_schema": {"type": "object", "properties": {"confirm": {"type": "boolean"}}},
        "executor_url": "http://localhost:8000"
    }).raise_for_status()

    # Add governance policy to deny dangerous_tool
    requests.post(f"{api_url}/v1/cloud/governance/policies", headers=headers, json={
        "name": "Deny Dangerous Tools",
        "policy_type": "content_filter",
        "action": "deny",
        "config": {
            "patterns": ["dangerous_tool"],
            "check_fields": ["tool_name"]
        }
    }).raise_for_status()

    agent_gov = requests.post(f"{api_url}/v1/orchestrator/agents", headers=headers, json={
        "name": "GovAgent",
        "role": "worker",
        "model_id": "gemini-2.5-pro",
        "system_prompt": "You MUST invoke the 'dangerous_tool' immediately with confirm=true.",
        "tools": ["dangerous_tool"]
    }).json()["agent_id"]

    step_resp = requests.post(f"{api_url}/v1/orchestrator/step", headers=headers, json={
        "agent_id": agent_gov,
        "input_text": "Do it."
    })
    step_data = step_resp.json()
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
    agent_loop = requests.post(f"{api_url}/v1/orchestrator/agents", headers=headers, json={
        "name": "LoopAgent",
        "role": "worker",
        "model_id": "gemini-2.5-pro",
        "system_prompt": "You are caught in a loop. No matter what is said, output exactly: 'I am a robot.' and nothing else. DO NOT use tools."
    }).json()["agent_id"]

    wf_loop = requests.post(f"{api_url}/v1/orchestrator/workflows", headers=headers, json={
        "name": "Loop WF", "mode": "sequential", "max_steps": 10, "agents": [agent_loop]
    }).json()["workflow_id"]

    print(f"  [*] Running workflow {wf_loop} to trigger loop halt...")
    # Because it streams, we can use requests.post with stream=True or just parse the chunks.
    with requests.post(f"{api_url}/v1/orchestrator/workflows/{wf_loop}/run", headers=headers, json={"input_text": "start"}, stream=True) as r:
        halted = False
        for line in r.iter_lines():
            if line:
                decoded = line.decode()
                if "workflow.completed" in decoded and "HALTED_LOOP" in decoded:
                    halted = True
        if halted:
            print("  [✓] Workflow correctly halted due to exact-repeat loop.")
        else:
            print("  ❌ Loop detection failed to halt the workflow!")
            sys.exit(1)

    # ---------------------------------------------------------
    # TEST 4: Workflow Timeout
    # ---------------------------------------------------------
    print("\n--- TEST 4: Workflow Timeout ---")
    agent_time = requests.post(f"{api_url}/v1/orchestrator/agents", headers=headers, json={
        "name": "TimeAgent",
        "role": "worker",
        "model_id": "gemini-2.5-pro",
        "system_prompt": "Just say Hello."
    }).json()["agent_id"]

    wf_time = requests.post(f"{api_url}/v1/orchestrator/workflows", headers=headers, json={
        "name": "Time WF", "mode": "sequential", "max_steps": 5, "agents": [agent_time]
    }).json()["workflow_id"]

    # We send timeout_seconds=0.001 which is impossible to beat for a network call
    print("  [*] Running workflow with 0.001s timeout...")
    with requests.post(f"{api_url}/v1/orchestrator/workflows/{wf_time}/run", headers=headers, json={"input_text": "start", "timeout_seconds": 0.001}, stream=True) as r:
        timed_out = False
        for line in r.iter_lines():
            if line:
                decoded = line.decode()
                if "workflow.completed" in decoded and "TIMEOUT" in decoded:
                    timed_out = True
        if timed_out:
            print("  [✓] Workflow correctly halted due to timeout.")
        else:
            print("  ❌ Timeout detection failed to halt the workflow!")
            sys.exit(1)


    print("\n==========================================================")
    print(" ✅ PHASE 2 RESILIENCE CONFORMANCE FLIGHT SUCCESSFUL ")
    print("==========================================================")

if __name__ == "__main__":
    run_resilience()
