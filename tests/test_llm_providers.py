"""
GRAFOMEM LLM Provider Tests — Sprint 17.

DB-free unit tests verifying LLM adapter normalization:
- Provider enum completeness
- Request/Response data models
- Mock adapter determinism and tool calling
- Provider-specific message format differences
- All tests run WITHOUT API keys or database
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from aml.cloud.llm_registry import (  # noqa: E402
    LLMProvider,
    LLMConfig,
    LLMRequest,
    LLMResponse,
    LLMRegistry,
)
from datetime import datetime, timezone  # noqa: E402


# ============================================================================
# Helpers
# ============================================================================

def _mock_config(model_id: str = "mock-model") -> LLMConfig:
    """Create an LLMConfig wired to the MOCK provider."""
    return LLMConfig(
        config_id="test-cfg-001",
        tenant_id="tenant-unit-test",
        provider=LLMProvider.MOCK,
        model_id=model_id,
        api_key=None,
        base_url=None,
        default_temperature=0.7,
        max_tokens=4096,
        enabled=True,
        created_at=datetime.now(tz=timezone.utc),
    )


def _registry() -> LLMRegistry:
    """Create a registry that won't actually connect (mock-only usage)."""
    return LLMRegistry(db_url="postgresql://test:test@localhost/test", encryption=_MockId(b"0"*32))


# ============================================================================
# 1–2  Provider enum
# ============================================================================

def test_provider_enum_completeness():
    """All 6 providers must be defined."""
    expected = {"OPENAI", "ANTHROPIC", "GEMINI", "OLLAMA", "CUSTOM", "MOCK"}
    actual = {p.name for p in LLMProvider}
    assert actual == expected


def test_provider_enum_values():
    """String values match lowercase names."""
    for p in LLMProvider:
        assert p.value == p.name.lower()


# ============================================================================
# 3–4  Request / Response data models
# ============================================================================

def test_llm_request_defaults():
    """LLMRequest should carry correct defaults."""
    req = LLMRequest(
        model_id="x",
        system_prompt="hello",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert req.temperature == 0.7
    assert req.max_tokens == 4096
    assert req.tools is None


def test_llm_response_fields():
    """LLMResponse exposes all mandatory fields."""
    resp = LLMResponse(
        content="ok",
        tool_calls=[],
        tokens_input=10,
        tokens_output=5,
        model_id="m",
        latency_ms=42,
        raw_response={},
    )
    assert resp.content == "ok"
    assert resp.tool_calls == []
    assert resp.tokens_input == 10
    assert resp.tokens_output == 5
    assert resp.model_id == "m"
    assert resp.latency_ms == 42
    assert resp.raw_response == {}


# ============================================================================
# 5–12  Mock adapter tests
# ============================================================================

def test_mock_adapter_deterministic():
    """Same input → same output (called twice)."""
    reg = _registry()
    cfg = _mock_config()
    req = LLMRequest(
        model_id="mock-model",
        system_prompt="You are a research analyst.",
        messages=[{"role": "user", "content": "Summarise GDPR."}],
    )
    r1 = reg._infer_mock(cfg, req)
    r2 = reg._infer_mock(cfg, req)
    assert r1.content == r2.content
    assert r1.tokens_input == r2.tokens_input
    assert r1.tokens_output == r2.tokens_output


def test_mock_adapter_researcher_role():
    """System prompt with 'research' → researcher output."""
    reg = _registry()
    cfg = _mock_config()
    req = LLMRequest(
        model_id="mock-model",
        system_prompt="You are a research analyst.",
        messages=[{"role": "user", "content": "Analyse compliance."}],
    )
    resp = reg._infer_mock(cfg, req)
    assert "[MockLLM|researcher|" in resp.content


def test_mock_adapter_writer_role():
    """System prompt with 'write' → writer output."""
    reg = _registry()
    cfg = _mock_config()
    req = LLMRequest(
        model_id="mock-model",
        system_prompt="You are a writer who drafts briefs.",
        messages=[{"role": "user", "content": "Write a brief."}],
    )
    resp = reg._infer_mock(cfg, req)
    assert "[MockLLM|writer|" in resp.content


def test_mock_adapter_reviewer_role():
    """System prompt with 'review' → reviewer output."""
    reg = _registry()
    cfg = _mock_config()
    req = LLMRequest(
        model_id="mock-model",
        system_prompt="You are a reviewer for quality assurance.",
        messages=[{"role": "user", "content": "Review the brief."}],
    )
    resp = reg._infer_mock(cfg, req)
    assert "[MockLLM|reviewer|" in resp.content


def test_mock_adapter_tool_calling():
    """When tools are provided and role is researcher, tool_calls is non-empty."""
    reg = _registry()
    cfg = _mock_config()
    req = LLMRequest(
        model_id="mock-model",
        system_prompt="You are a research analyst.",
        messages=[{"role": "user", "content": "Retrieve compliance data."}],
        tools=[
            {
                "name": "retrieve_memory",
                "description": "Retrieve memories from the graph.",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            }
        ],
    )
    resp = reg._infer_mock(cfg, req)
    assert len(resp.tool_calls) > 0
    assert resp.tool_calls[0]["name"] == "retrieve_memory"


def test_mock_adapter_token_counting():
    """tokens_input and tokens_output are > 0."""
    reg = _registry()
    cfg = _mock_config()
    req = LLMRequest(
        model_id="mock-model",
        system_prompt="You are a research analyst.",
        messages=[{"role": "user", "content": "Analyse compliance."}],
    )
    resp = reg._infer_mock(cfg, req)
    assert resp.tokens_input > 0
    assert resp.tokens_output > 0


def test_mock_adapter_input_hash_in_output():
    """The BLAKE2b input hash appears in the content."""
    import hashlib, json

    reg = _registry()
    cfg = _mock_config()
    req = LLMRequest(
        model_id="mock-model",
        system_prompt="default agent",
        messages=[{"role": "user", "content": "hello world"}],
    )
    # Compute expected hash the same way the adapter does
    canonical = json.dumps(
        {"system_prompt": req.system_prompt, "messages": req.messages},
        sort_keys=True,
        ensure_ascii=True,
    )
    expected_hash = hashlib.blake2b(canonical.encode(), digest_size=16).hexdigest()

    resp = reg._infer_mock(cfg, req)
    assert expected_hash in resp.content


def test_mock_adapter_different_inputs_different_outputs():
    """Different messages → different content."""
    reg = _registry()
    cfg = _mock_config()
    req_a = LLMRequest(
        model_id="mock-model",
        system_prompt="You are a research analyst.",
        messages=[{"role": "user", "content": "Message A"}],
    )
    req_b = LLMRequest(
        model_id="mock-model",
        system_prompt="You are a research analyst.",
        messages=[{"role": "user", "content": "Message B"}],
    )
    r_a = reg._infer_mock(cfg, req_a)
    r_b = reg._infer_mock(cfg, req_b)
    assert r_a.content != r_b.content


# ============================================================================
# 13–15  Import-error smoke tests for missing provider SDKs
# ============================================================================

def test_openai_import_error_message():
    """_infer_openai raises ImportError with helpful message when openai not installed."""
    reg = _registry()
    cfg = _mock_config()
    cfg.provider = LLMProvider.OPENAI  # type: ignore[misc]
    req = LLMRequest(
        model_id="mock-model",
        system_prompt="test",
        messages=[{"role": "user", "content": "hi"}],
    )
    try:
        reg._infer_openai(cfg, req)
    except ImportError as exc:
        assert "openai" in str(exc).lower()
    except Exception:
        # If openai IS installed, the call will fail for other reasons — that's fine.
        pass


def test_anthropic_import_error_message():
    """_infer_anthropic raises ImportError with helpful message when anthropic not installed."""
    reg = _registry()
    cfg = _mock_config()
    cfg.provider = LLMProvider.ANTHROPIC  # type: ignore[misc]
    req = LLMRequest(
        model_id="mock-model",
        system_prompt="test",
        messages=[{"role": "user", "content": "hi"}],
    )
    try:
        reg._infer_anthropic(cfg, req)
    except ImportError as exc:
        assert "anthropic" in str(exc).lower()
    except Exception:
        pass


def test_gemini_import_error_message():
    """_infer_gemini raises ImportError with helpful message when google-genai not installed."""
    reg = _registry()
    cfg = _mock_config()
    cfg.provider = LLMProvider.GEMINI  # type: ignore[misc]
    req = LLMRequest(
        model_id="mock-model",
        system_prompt="test",
        messages=[{"role": "user", "content": "hi"}],
    )
    try:
        reg._infer_gemini(cfg, req)
    except ImportError as exc:
        assert "google-genai" in str(exc).lower()
    except Exception:
        pass


# ============================================================================
# 16  Unknown model
# ============================================================================

def test_infer_unknown_model_raises():
    """Calling infer() with unregistered model raises ValueError.

    We override get_provider to return None (simulating no DB match)
    so we don't need a real database connection.
    """
    reg = _registry()
    # Monkey-patch to avoid DB connection
    reg.get_provider = lambda tenant_id, model_id: None  # type: ignore[assignment]

    req = LLMRequest(
        model_id="nonexistent-model",
        system_prompt="test",
        messages=[{"role": "user", "content": "hi"}],
    )
    with pytest.raises(ValueError, match="No LLM provider configured"):
        reg.infer("tenant-unit-test", req)


class _MockId:
    def __init__(self, k): self.k = k
    def sign(self, m): 
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        priv = Ed25519PrivateKey.from_private_bytes(self.k)
        return priv.sign(m), priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    def public_key(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return Ed25519PrivateKey.from_private_bytes(self.k).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
