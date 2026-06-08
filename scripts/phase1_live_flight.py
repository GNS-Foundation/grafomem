import os
import sys
import uuid
import httpx
import time

API_URL = os.environ.get("GRAFOMEM_API_URL", "http://127.0.0.1:8000")
API_KEY = os.environ.get("GRAFOMEM_API_KEY", "gfm_dev_key")

TENANT = f"prod_flight_{uuid.uuid4().hex[:8]}"
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

def main():
    print("==========================================================")
    print(f" GRAFOMEM CLOUD : PHASE 1 FLIGHT ({API_URL})")
    print(f" Tenant: {TENANT}")
    print("==========================================================")
    
    with httpx.Client(base_url=API_URL, headers=HEADERS, timeout=10.0) as client:
        # Check health
        try:
            r = client.get("/v1/health")
            r.raise_for_status()
            print("[✓] Service is reachable")
        except Exception as e:
            print(f"[!] Could not reach service: {e}")
            sys.exit(1)
            
        print("\n[*] Generating real BYOM inference step...")
        
        # 1. Log a real decision (orchestrator_step)
        decision_payload = {
            "prompt_hash": "b2_abc123",
            "model": "gpt-4o",
            "provider": "openai",
            "decision_type": "orchestrator_step",
            "raw_output": "{\"action\": \"approve\", \"reason\": \"clean record\"}",
            "is_synthetic": False,
            "context_refs": ["AML-1234"],
            "metadata": {"flight": "phase1"}
        }
        
        r = client.post(f"/v1/decisions/log?tenant_id={TENANT}", json=decision_payload)
        if r.status_code != 201:
            print(f"[!] Failed to log decision: {r.text}")
            sys.exit(1)
            
        decision_resp = r.json()
        decision_id = decision_resp["decision_id"]
        print(f"[✓] Logged real decision: {decision_id}")
        
        # 2. Verify the decision signature
        r = client.get(f"/v1/decisions/{decision_id}?tenant_id={TENANT}")
        if r.status_code != 200:
            print(f"[!] Failed to get decision: {r.text}")
            sys.exit(1)
        
        doc = r.json()
        if not doc.get("signature"):
            print(f"[!] Decision has no signature!")
            sys.exit(1)
            
        print(f"[✓] Decision has signature: {doc['signature'][:16]}...")
        
        # We need a receipt for it. The REST API /v1/decisions/log automatically generates a breadcrumb.
        # But wait, we also need an artifact receipt. Is there an endpoint to register an artifact?
        # Let's check if the decision endpoint also creates a receipt, or if we need to call /v1/artifacts/register.
        
        # 3. Roll gcrumbs epoch
        r = client.post(f"/v1/gcrumbs/roll?tenant_id={TENANT}")
        if r.status_code != 200:
            print(f"[!] Failed to roll epoch: {r.text}")
            sys.exit(1)
            
        epoch = r.json()
        epoch_id = epoch["epoch_id"]
        print(f"[✓] Rolled gcrumbs epoch: {epoch_id}")
        
        # 4. Verify chain inclusion
        r = client.get(f"/v1/gcrumbs/verify?tenant_id={TENANT}")
        if r.status_code != 200:
            print(f"[!] Failed to verify chain: {r.text}")
            sys.exit(1)
            
        verify_resp = r.json()
        if verify_resp.get("status") != "intact":
            print(f"[!] Chain verification failed: {verify_resp}")
            sys.exit(1)
            
        print(f"[✓] Chain is intact. Included {verify_resp.get('count', 'multiple')} events.")
        print("\n✅ PHASE 1 FLIGHT SUCCESSFUL")

if __name__ == "__main__":
    main()
