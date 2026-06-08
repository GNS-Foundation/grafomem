import asyncio
import os
import sys
import uuid
import httpx
from datetime import datetime
from pydantic import BaseModel
from aml.sdk_v2 import GrafomemAsyncClient
from aml.types import OrchestratorStep, TrustTier, AgentPrincipal

API_URL = os.environ.get("GRAFOMEM_API_URL", "http://127.0.0.1:8000")
API_KEY = os.environ.get("GRAFOMEM_API_KEY", "gfm_dev_key")

TENANT = f"prod_flight_{uuid.uuid4().hex[:8]}"

async def main():
    print("==========================================================")
    print(f" GRAFOMEM CLOUD : PHASE 1 FLIGHT ({API_URL})")
    print(f" Tenant: {TENANT}")
    print("==========================================================")
    
    # Initialize the v2 async SDK client
    client = GrafomemAsyncClient(
        base_url=API_URL,
        api_key=API_KEY,
        tenant_id=TENANT
    )
    
    async with client:
        # Check health
        try:
            health = await client._client.get(f"{API_URL}/v1/health")
            health.raise_for_status()
            print("[✓] Service is reachable")
        except Exception as e:
            print(f"[!] Could not reach service: {e}")
            sys.exit(1)
            
        print("\n[*] Generating real BYOM inference step...")
        
        # We need a payload that fits the generic LogDecisionRequest schema
        # For orchestrator_steps, the generic POST /v1/decisions/log handles it!
        # The schema is `LogDecisionRequest` in app.py.
        # Wait, the v2 SDK has log_decision or similar? Let's check sdk_v2.py...
        pass

if __name__ == "__main__":
    asyncio.run(main())
