import sys
import psycopg
import json
import hashlib
from aml.cloud.replay_engine import ReplayEngine
from aml.cloud.decision_trail import DecisionTrailService
from aml.cloud.tenant_key_manager import TenantKeyManager
from pydantic import BaseModel
from typing import Any

class LLMResponse(BaseModel):
    content: str
    raw_response: Any
    tokens_input: int
    tokens_output: int
    model_id: str
    latency_ms: int
    tool_calls: list = []

db_url = "postgresql://grafomem:dev@localhost:5432/grafomem"
dt = DecisionTrailService(db_url)
tkm = TenantKeyManager("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", db_url)

class DummyLLMReg:
    def list_providers(self, tenant_id):
        class P: model_id = "mock-model"
        return [P()]
    def get_provider(self, *args, **kwargs):
        class DummyProv:
            def invoke(self, req):
                import sys
                sys.path.append('tests')
                import mock_llm
                res = mock_llm.mock_infer(
                    req.model_id, req.system_prompt, req.messages,
                    req.tools, req.temperature, req.max_tokens
                )
                return LLMResponse(**res)
        return DummyProv()
    def infer(self, tenant_id, request):
        return self.get_provider().invoke(request)

def run():
    llm_reg = DummyLLMReg()
    re = ReplayEngine(db_url, decision_trail=dt, encryption=tkm, llm_registry=llm_reg)

    with psycopg.connect(db_url) as conn:
        row = conn.execute("SELECT decision_id, tenant_id FROM decision_records ORDER BY created_at DESC LIMIT 1").fetchone()
        decision_id, tenant_id = row
        print(f"Replaying {decision_id} for tenant {tenant_id}")
        
        dec = dt.get(decision_id, encryption=tkm)
        verdict = re.replay(decision_id, tenant_id)
        
        orig = dec.raw_output
        rep = verdict.replayed_output
        
        import re
        orig_match = re.search(r'\[MockLLM\|[^\|]+\|([a-f0-9]+)\]', orig)
        orig_hash = orig_match.group(1) if orig_match else "UNKNOWN"
        rep_match = re.search(r'\[MockLLM\|[^\|]+\|([a-f0-9]+)\]', rep)
        rep_hash = rep_match.group(1) if rep_match else "UNKNOWN"
        
        print("ORIGINAL HASH:", orig_hash)
        print("REPLAYED HASH:", rep_hash)
        
        print("ORIGINAL OUTPUT:", repr(orig))
        print("REPLAYED OUTPUT:", repr(rep))

if __name__ == "__main__":
    run()
