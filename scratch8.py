from src.aml.cloud.llm_registry import LLMRegistry, LLMRequest
import dataclasses
import json

@dataclasses.dataclass
class MockConfig:
    api_key: str
    model_id: str = "gpt-4o"
    provider: str = "openai"

req = LLMRequest(
    model_id="gpt-4o",
    messages=[{"role": "user", "content": "hello"}],
    system_prompt="sys",
    temperature=0.0,
    max_tokens=100,
    tools=None
)

registry = LLMRegistry(db_url=None) # mock
config_invalid = MockConfig(api_key="sk-invalid-key")

try:
    print("[+] Executing step with INVALID key (sk-invalid-key)")
    registry._infer_openai(config_invalid, req)
except Exception as e:
    print(f"FAILURE RESPONSE (invalid key):\n{type(e).__name__}: {str(e)}")

try:
    print("\n[+] Executing step with EMPTY key")
    config_empty = MockConfig(api_key="")
    registry._infer_openai(config_empty, req)
except Exception as e:
    print(f"FAILURE RESPONSE (empty key):\n{type(e).__name__}: {str(e)}")

