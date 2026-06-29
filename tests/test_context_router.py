import pytest
import hashlib
from aml.cloud.llm_registry import LLMRegistry, LLMProvider

class MockEncryption:
    def encrypt(self, data: bytes) -> bytes: return data
    def decrypt(self, data: bytes) -> bytes: return data

from tests.conftest import TEST_DB_URL

def test_isolation_invariant(monkeypatch):
    registry = LLMRegistry(TEST_DB_URL, encryption=MockEncryption())
    registry.ensure_schema()
    registry.register_provider("t1", "anthropic", "claude-3-haiku-20240307", api_key="test")
    
    captured_kwargs = {}
    def mock_messages_create(self, **kwargs):
        captured_kwargs.update(kwargs)
        class MockMsg:
            content = [type('T', (), {'type': 'text', 'text': 'ok'})()]
            stop_reason = 'end_turn'
            usage = type('U', (), {'input_tokens': 100, 'output_tokens': 10, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 100})()
            id = 'mock_id'
            model = 'mock_model'
        return MockMsg()

    monkeypatch.setattr("anthropic.resources.messages.Messages.create", mock_messages_create)
    
    system_prompt = "You are an agent."
    messages = [{"role": "user", "content": "Here is tenant private data: secret123"}]
    tools = [{"name": "get_weather", "description": "Get weather", "input_schema": {"type": "object", "properties": {}}}]
    
    from aml.cloud.llm_registry import LLMRequest
    req = LLMRequest(model_id="claude-3-haiku-20240307", system_prompt=system_prompt, messages=messages, tools=tools)
    registry.infer("t1", req)
    
    # 1. Assert cache_control is on the LAST tool
    assert "tools" in captured_kwargs
    assert "cache_control" in captured_kwargs["tools"][-1]
    assert captured_kwargs["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    
    # 2. Assert tenant data is in messages, NOT in system prompt
    assert "secret123" not in str(captured_kwargs.get("system", ""))
    assert "secret123" in str(captured_kwargs.get("messages", ""))

def test_deterministic_ordering():
    from aml.cloud.orchestrator import OrchestratorService
    from aml.backends.interface import Memory
    
    f1 = {"content": "B", "ref": 1}
    f2 = {"content": "A", "ref": 2}
    
    o = OrchestratorService(None, None, None, None)
    
    msg1 = o._build_messages(None, "user input", [f1, f2])
    msg2 = o._build_messages(None, "user input", [f2, f1])
    
    assert msg1 == msg2

def test_caching_transparency():
    pass
