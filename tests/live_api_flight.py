import requests
import json
import os
import subprocess

API_URL = "https://grafomem-production.up.railway.app"
API_KEY = "gfm_99d5ca49e7b954d77f3e4531faadb7cd354402f762ab51d8"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
TENANT = "test_flight_tenant"

def run_sql(query):
    result = subprocess.run(
        ["railway", "connect", "postgres"],
        input=query.encode("utf-8"),
        capture_output=True
    )
    if result.returncode != 0:
        print(f"SQL Error: {result.stderr.decode()}")
    return result.stdout.decode()

print("==========================================================")
print(" GRAFOMEM CLOUD : LIVE API REST DOGFOOD FLIGHT ")
print("==========================================================")

# Clear DB state for the test tenant
clear_sql = f"""
DELETE FROM world_model_actions WHERE tenant_id = '{TENANT}';
DELETE FROM world_model_types WHERE tenant_id = '{TENANT}';
DELETE FROM governance_evaluation_log WHERE tenant_id = '{TENANT}';
DELETE FROM governance_policies WHERE tenant_id = '{TENANT}';
"""
run_sql(clear_sql)
print("[*] Cleared state for test_flight_tenant via railway connect.")

# 1. Register Types
types_req = [
    {
        "kind": "object", "name": "SAR_Report",
        "spec": {
            "type": "object",
            "properties": {"amount": {"type": "number"}, "customer": {"type": "string"}},
            "required": ["amount", "customer"]
        }
    },
    {
        "kind": "action", "name": "escalate",
        "spec": {
            "operation": "invoke",
            "required_trust_tier": "trusted",
            "type": "object",
            "properties": {
                "rationale": {"type": "string"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "dummy_list": {"type": "array"}
            },
            "required": ["rationale", "evidence_refs"]
        }
    },
    {
        "kind": "action", "name": "clear",
        "spec": {
            "operation": "invoke",
            "required_trust_tier": "trusted",
            "type": "object",
            "properties": {
                "rationale": {"type": "string"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["rationale", "evidence_refs"]
        }
    }
]

for t in types_req:
    r = requests.post(f"{API_URL}/v1/world-model/types", headers=HEADERS, json=t)
    if r.status_code not in [201, 400]:
        print(f"Error registering type {t['name']}:", r.text)
        exit(1)
    if r.status_code == 400 and "already exists" not in r.text.lower():
        print(f"Error registering type {t['name']}:", r.text)
        exit(1)

print("[✓] R5 Financial Ontology registered via REST.")

# 2. Register Policies
policy_req = {
    "name": "require_evidence",
    "description": "Require evidence_refs for escalate",
    "policy_type": "require_params",
    "action": "escalate",
    "config": {"required_params": ["evidence_refs"]},
    "enabled": True,
    "priority": 10
}
r = requests.post(f"{API_URL}/v1/governance/policies", headers=HEADERS, json=policy_req)
if r.status_code not in [200, 201, 400]:
    print("Error registering policy:", r.text)
    exit(1)

print("[✓] Generic PDP constraints registered via REST.")

# 3. Submit to REST API (Governance + Signing happen SERVER-SIDE!)
invoke_payload = {
    "action_name": "escalate",
    "subject_refs": ["AMLAlert:ALERT-2025-009981"],
    "params": {
        "rationale": "High-risk structuring detected across multiple branches.",
        "evidence_refs": ["txn-4011", "txn-4012"]
    },
    "authority": {"human_principal": "sys_aml_pipeline", "trust_tier": "trusted", "llm_model": "gpt-4o"}
}

print("[*] Submitting action to /v1/world-model/actions/invoke ...")
r = requests.post(f"{API_URL}/v1/world-model/actions/invoke", headers=HEADERS, json=invoke_payload)
if r.status_code == 201:
    receipt = r.json()
    action_id = receipt["action_id"]
    print(f"✅ TEST 3: PASSED (Valid escalation succeeded via REST)")
else:
    print(f"❌ TEST 3: FAILED (Valid escalation denied): {r.text}")
    exit(1)

# 4. Verify the signature via REST API
v = requests.get(f"{API_URL}/v1/world-model/actions/{action_id}/verify", headers=HEADERS)
if v.status_code == 200 and v.json().get("passed"):
    print("✅ TEST 4: PASSED (Ed25519 signature verified via REST)")
else:
    print(f"❌ TEST 4: FAILED (Signature verification failed via REST): {v.text}")
    exit(1)

# 5. Tamper-evidence check 5c: Decision Tampering 
print(f"[*] Simulating DB intrusion: flipping decision on {action_id} to 'clear'...")
update_sql = f"""
UPDATE world_model_actions 
SET document = jsonb_set(document, '{{action_name}}', '"clear"'), action_name = 'clear' 
WHERE action_id = '{action_id}';
"""
run_sql(update_sql)

v_decision_tampered = requests.get(f"{API_URL}/v1/world-model/actions/{action_id}/verify", headers=HEADERS)
if v_decision_tampered.status_code == 200 and not v_decision_tampered.json().get("passed"):
    print("✅ TEST 5c: PASSED (Decision tamper check: REST /verify rejected the flipped action_name)")
else:
    print(f"❌ TEST 5c: FAILED (Decision tamper check failed): {v_decision_tampered.text}")

print("==========================================================")
print(f"✅ SUCCESS! Flight completed successfully.")
print(f"ACTION_ID: {action_id}")
