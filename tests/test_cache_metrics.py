from aml.cloud.llm_registry import LLMRegistry

class MockEncryption:
    def encrypt(self, data: bytes) -> bytes: return data
    def decrypt(self, data: bytes) -> bytes: return data

from tests.conftest import TEST_DB_URL

def test_mock_cache_metrics_parsing():
    registry = LLMRegistry(TEST_DB_URL, encryption=MockEncryption())
    registry.ensure_schema()
    registry.register_provider("t1", "mock", "mock-model")
    
    system_prompt = 'You are an agent with "cache_control" enabled.'
    messages = [{"role": "user", "content": "hello"}]
    
    from aml.cloud.llm_registry import LLMRequest
    req = LLMRequest(model_id="mock-model", system_prompt=system_prompt, messages=messages)
    resp = registry.infer("t1", req)
    
    # Because 'cache_control' is in the canonical input hash check inside mock_infer, 
    # mock_llm will simulate a cache hit.
    assert resp.tokens_cached_read == 400
    assert resp.tokens_cached_create == 0

def test_mock_cache_metrics_miss():
    registry = LLMRegistry(TEST_DB_URL, encryption=MockEncryption())
    registry.ensure_schema()
    registry.register_provider("t1", "mock", "mock-model")
    
    system_prompt = 'No caching here.'
    messages = [{"role": "user", "content": "hello"}]
    
    from aml.cloud.llm_registry import LLMRequest
    req = LLMRequest(model_id="mock-model", system_prompt=system_prompt, messages=messages)
    resp = registry.infer("t1", req)
    
    assert resp.tokens_cached_read == 0
    assert resp.tokens_cached_create == 400
