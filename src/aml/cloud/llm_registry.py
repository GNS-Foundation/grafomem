"""
GRAFOMEM LLM Registry — Bring Your Own Model abstraction layer.

Unified interface for calling LLMs from any provider: OpenAI, Anthropic,
Google Gemini, Ollama (local), or any OpenAI-compatible endpoint.

Every provider's tool-calling format is normalized into a single interface:
  - Tools defined as {name, description, input_schema}
  - Tool calls returned as [{name, arguments}]
  - System prompts handled per-provider (Anthropic separates them)

The registry stores per-tenant provider configurations (model_id → config)
in PostgreSQL, with API keys stored alongside.

Backed by PostgreSQL via psycopg v3 (sync), following the same patterns
as GovernanceGateway and OrchestratorService.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.llm_registry")


# ============================================================================
# Enumerations
# ============================================================================

class LLMProvider(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    OLLAMA = "ollama"
    CUSTOM = "custom"  # Any OpenAI-compatible endpoint
    MOCK = "mock"      # Deterministic mock for testing


# ============================================================================
# Core data types
# ============================================================================

@dataclass(slots=True)
class LLMConfig:
    """Configuration for a registered LLM provider."""
    config_id: str
    tenant_id: str
    provider: LLMProvider
    model_id: str
    api_key: str | None
    base_url: str | None
    default_temperature: float
    max_tokens: int
    enabled: bool
    created_at: datetime


@dataclass(slots=True)
class LLMRequest:
    """Normalized LLM inference request."""
    model_id: str
    system_prompt: str
    messages: list[dict[str, str]]  # [{role, content}]
    tools: list[dict] | None = None  # Normalized tool definitions
    temperature: float = 0.7
    max_tokens: int = 4096


@dataclass(slots=True)
class LLMResponse:
    """Normalized LLM inference response."""
    content: str
    tool_calls: list[dict]  # [{name, arguments}]
    tokens_input: int
    tokens_output: int
    model_id: str
    latency_ms: int
    raw_response: dict  # Provider-specific raw data


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS llm_providers (
    config_id       TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    api_key         TEXT,
    base_url        TEXT,
    temperature     REAL NOT NULL DEFAULT 0.7,
    max_tokens      INTEGER NOT NULL DEFAULT 4096,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, model_id)
);
CREATE INDEX IF NOT EXISTS idx_lp_tenant
    ON llm_providers(tenant_id, enabled);
"""


# ============================================================================
# LLMRegistry
# ============================================================================

class LLMRegistry:
    """Manages LLM provider configurations and provides unified inference.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    """

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url
        self._conn: psycopg.Connection[dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    def ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.execute(_SCHEMA_SQL)
        logger.info("LLM Registry schema ensured")

    # ------------------------------------------------------------------
    # Provider CRUD
    # ------------------------------------------------------------------

    def register_provider(
        self,
        tenant_id: str,
        provider: LLMProvider | str,
        model_id: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_temperature: float = 0.7,
        max_tokens: int = 4096,
        enabled: bool = True,
    ) -> LLMConfig:
        """Register or update an LLM provider for a tenant."""
        now = datetime.now(tz=timezone.utc)
        config_id = uuid.uuid4().hex[:24]

        if isinstance(provider, str):
            provider = LLMProvider(provider)

        conn = self._get_conn()

        # Upsert: delete existing config for this tenant+model
        conn.execute(
            "DELETE FROM llm_providers WHERE tenant_id = %s AND model_id = %s",
            (tenant_id, model_id),
        )

        conn.execute(
            "INSERT INTO llm_providers "
            "(config_id, tenant_id, provider, model_id, api_key, base_url, "
            " temperature, max_tokens, enabled, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                config_id, tenant_id, provider.value, model_id,
                api_key, base_url, default_temperature, max_tokens,
                enabled, now,
            ),
        )

        logger.info(
            "LLM provider registered: %s/%s for tenant %s",
            provider.value, model_id, tenant_id,
        )

        return LLMConfig(
            config_id=config_id,
            tenant_id=tenant_id,
            provider=provider,
            model_id=model_id,
            api_key=api_key,
            base_url=base_url,
            default_temperature=default_temperature,
            max_tokens=max_tokens,
            enabled=enabled,
            created_at=now,
        )

    def list_providers(self, tenant_id: str) -> list[LLMConfig]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM llm_providers WHERE tenant_id = %s ORDER BY created_at",
            (tenant_id,),
        ).fetchall()
        return [self._row_to_config(r) for r in rows]

    def get_provider(self, tenant_id: str, model_id: str) -> LLMConfig | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM llm_providers "
            "WHERE tenant_id = %s AND model_id = %s AND enabled = TRUE",
            (tenant_id, model_id),
        ).fetchone()
        return self._row_to_config(row) if row else None

    def delete_provider(self, tenant_id: str, model_id: str) -> bool:
        conn = self._get_conn()
        result = conn.execute(
            "DELETE FROM llm_providers WHERE tenant_id = %s AND model_id = %s",
            (tenant_id, model_id),
        )
        return result.rowcount > 0

    # ------------------------------------------------------------------
    # Inference — THE CORE METHOD
    # ------------------------------------------------------------------

    def infer(self, tenant_id: str, request: LLMRequest) -> LLMResponse:
        """Call an LLM with the normalized interface.

        Dispatches to the correct provider adapter:
          - OpenAI:    openai.chat.completions.create()
          - Anthropic: anthropic.messages.create()
          - Gemini:    google.genai GenerativeModel
          - Ollama:    HTTP POST to localhost
          - Custom:    OpenAI-compatible endpoint
        """
        config = self.get_provider(tenant_id, request.model_id)
        if config is None:
            raise ValueError(
                f"No LLM provider configured for model '{request.model_id}' "
                f"(tenant={tenant_id}). Register one via POST /v1/llm/providers."
            )

        t0 = time.monotonic()

        if config.provider == LLMProvider.OPENAI:
            response = self._infer_openai(config, request)
        elif config.provider == LLMProvider.ANTHROPIC:
            response = self._infer_anthropic(config, request)
        elif config.provider == LLMProvider.GEMINI:
            response = self._infer_gemini(config, request)
        elif config.provider == LLMProvider.OLLAMA:
            response = self._infer_ollama(config, request)
        elif config.provider == LLMProvider.CUSTOM:
            response = self._infer_custom(config, request)
        elif config.provider == LLMProvider.MOCK:
            response = self._infer_mock(config, request)
        else:
            raise ValueError(f"Unknown provider: {config.provider}")

        response.latency_ms = int((time.monotonic() - t0) * 1000)

        logger.info(
            "LLM inference: model=%s provider=%s tokens=%d latency=%dms",
            request.model_id, config.provider.value,
            response.tokens_output, response.latency_ms,
        )

        return response

    # ------------------------------------------------------------------
    # Provider adapters
    # ------------------------------------------------------------------

    def _infer_openai(self, config: LLMConfig, request: LLMRequest) -> LLMResponse:
        """OpenAI ChatCompletion API."""
        try:
            import openai
        except ImportError:
            raise ImportError(
                "openai package required for OpenAI provider. "
                "Install with: pip install openai"
            )

        client = openai.OpenAI(api_key=config.api_key)

        kwargs: dict[str, Any] = {
            "model": config.model_id,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                *request.messages,
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }

        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {"type": "object"}),
                        "strict": True,
                    },
                }
                for t in request.tools
            ]

        response = client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        content = choice.message.content or ""
        tool_calls = []

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                })

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            tokens_input=response.usage.prompt_tokens if response.usage else 0,
            tokens_output=response.usage.completion_tokens if response.usage else 0,
            model_id=config.model_id,
            latency_ms=0,
            raw_response={"id": response.id, "model": response.model},
        )

    def _infer_anthropic(self, config: LLMConfig, request: LLMRequest) -> LLMResponse:
        """Anthropic Messages API."""
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "anthropic package required for Anthropic provider. "
                "Install with: pip install anthropic"
            )

        client = anthropic.Anthropic(api_key=config.api_key)

        kwargs: dict[str, Any] = {
            "model": config.model_id,
            "system": request.system_prompt,
            "messages": request.messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }

        if request.tools:
            kwargs["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("input_schema", {"type": "object"}),
                }
                for t in request.tools
            ]

        response = client.messages.create(**kwargs)

        content = ""
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "name": block.name,
                    "arguments": block.input,
                })

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            tokens_input=response.usage.input_tokens,
            tokens_output=response.usage.output_tokens,
            model_id=config.model_id,
            latency_ms=0,
            raw_response={"id": response.id, "model": response.model},
        )

    def _infer_gemini(self, config: LLMConfig, request: LLMRequest) -> LLMResponse:
        """Google Gemini API via google-genai SDK."""
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise ImportError(
                "google-genai package required for Gemini provider. "
                "Install with: pip install google-genai"
            )

        client = genai.Client(api_key=config.api_key)

        # Build contents from messages
        contents = []
        for msg in request.messages:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(
                types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=msg["content"])],
                )
            )

        # Build tool declarations
        tools = None
        if request.tools:
            func_declarations = []
            for t in request.tools:
                func_declarations.append(
                    types.FunctionDeclaration(
                        name=t["name"],
                        description=t.get("description", ""),
                        parameters=t.get("input_schema"),
                    )
                )
            tools = [types.Tool(function_declarations=func_declarations)]

        config_obj = types.GenerateContentConfig(
            system_instruction=request.system_prompt,
            temperature=request.temperature,
            max_output_tokens=request.max_tokens,
            tools=tools,
        )

        response = client.models.generate_content(
            model=config.model_id,
            contents=contents,
            config=config_obj,
        )

        content = ""
        tool_calls = []

        if response.candidates:
            for part in response.candidates[0].content.parts:
                if part.text:
                    content += part.text
                elif part.function_call:
                    tool_calls.append({
                        "name": part.function_call.name,
                        "arguments": dict(part.function_call.args) if part.function_call.args else {},
                    })

        tokens_in = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
        tokens_out = getattr(response.usage_metadata, "candidates_token_count", 0) or 0

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            model_id=config.model_id,
            latency_ms=0,
            raw_response={},
        )

    def _infer_ollama(self, config: LLMConfig, request: LLMRequest) -> LLMResponse:
        """Ollama local inference via HTTP API."""
        try:
            import httpx
        except ImportError:
            raise ImportError(
                "httpx package required for Ollama provider. "
                "Install with: pip install httpx"
            )

        base_url = config.base_url or "http://localhost:11434"

        messages = [
            {"role": "system", "content": request.system_prompt},
            *request.messages,
        ]

        payload: dict[str, Any] = {
            "model": config.model_id,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "num_predict": request.max_tokens,
            },
        }

        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {"type": "object"}),
                    },
                }
                for t in request.tools
            ]

        with httpx.Client(timeout=120.0) as client:
            resp = client.post(f"{base_url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()

        content = data.get("message", {}).get("content", "")
        tool_calls = []

        for tc in data.get("message", {}).get("tool_calls", []):
            func = tc.get("function", {})
            tool_calls.append({
                "name": func.get("name", ""),
                "arguments": func.get("arguments", {}),
            })

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            tokens_input=data.get("prompt_eval_count", 0),
            tokens_output=data.get("eval_count", 0),
            model_id=config.model_id,
            latency_ms=0,
            raw_response={"model": data.get("model")},
        )

    def _infer_custom(self, config: LLMConfig, request: LLMRequest) -> LLMResponse:
        """Any OpenAI-compatible endpoint (vLLM, LiteLLM, etc.)."""
        try:
            import openai
        except ImportError:
            raise ImportError(
                "openai package required for custom OpenAI-compatible provider. "
                "Install with: pip install openai"
            )

        client = openai.OpenAI(
            api_key=config.api_key or "not-needed",
            base_url=config.base_url,
        )

        kwargs: dict[str, Any] = {
            "model": config.model_id,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                *request.messages,
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }

        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {"type": "object"}),
                    },
                }
                for t in request.tools
            ]

        response = client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        content = choice.message.content or ""
        tool_calls = []

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                })

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            tokens_input=response.usage.prompt_tokens if response.usage else 0,
            tokens_output=response.usage.completion_tokens if response.usage else 0,
            model_id=config.model_id,
            latency_ms=0,
            raw_response={"id": response.id},
        )

    # ------------------------------------------------------------------
    # Mock adapter (deterministic, for testing)
    # ------------------------------------------------------------------

    def _infer_mock(self, config: LLMConfig, request: LLMRequest) -> LLMResponse:
        """Deterministic mock LLM — returns f(input), not canned-by-role.

        The response is a deterministic function of (system_prompt + messages).
        A canonical hash of the input is embedded in the output, so replay
        correctly reports DIVERGED when input reconstruction is wrong.
        """
        # Build canonical input hash
        canonical = json.dumps({
            "system_prompt": request.system_prompt,
            "messages": request.messages,
        }, sort_keys=True, ensure_ascii=True)
        input_hash = hashlib.blake2b(
            canonical.encode(), digest_size=16,
        ).hexdigest()

        # Detect role from system prompt
        sp_lower = request.system_prompt.lower()
        tool_calls: list[dict] = []

        # Token counting (deterministic)
        input_chars = len(request.system_prompt) + sum(
            len(m.get("content", "")) for m in request.messages
        )
        tokens_input = max(20, input_chars // 4)

        if "research" in sp_lower or "analyst" in sp_lower:
            content = (
                f"[MockLLM|researcher|{input_hash}] "
                f"Based on the retrieved compliance data, the key findings are: "
                f"1) GDPR Article 17 requires right to erasure with cryptographic proof. "
                f"2) EU AI Act Article 12 mandates logging of all AI decisions. "
                f"3) Rate limiting prevents abuse of agent resources. "
                f"Input fingerprint: {input_hash}. Analysis complete."
            )
            # Return a tool_call to exercise Step 4 of execute_step
            if request.tools:
                for t in request.tools:
                    if "retrieve" in t.get("name", "").lower():
                        tool_calls = [{
                            "name": t["name"],
                            "arguments": {"query": "GDPR compliance requirements"},
                        }]
                        break
            tokens_output = 85

        elif "writ" in sp_lower or "draft" in sp_lower:
            content = (
                f"[MockLLM|writer|{input_hash}] "
                f"## Compliance Brief\n\n"
                f"Key regulatory requirements:\n"
                f"- **GDPR Art. 17**: Right to erasure must be implemented.\n"
                f"- **EU AI Act Art. 12**: All inference decisions must be logged.\n"
                f"- **Rate Limiting**: Enforced at the governance layer.\n\n"
                f"Recommendation: Deploy GRAFOMEM governance stack. "
                f"Input fingerprint: {input_hash}."
            )
            tokens_output = 92

        elif "review" in sp_lower or "quality" in sp_lower or "score" in sp_lower:
            content = (
                f"[MockLLM|reviewer|{input_hash}] "
                f"Score: 8/10. The brief accurately covers GDPR and EU AI Act. "
                f"Strengths: Clear structure, regulatory citations. "
                f"Improvement: Add DORA Art. 6 coverage for financial services. "
                f"Input fingerprint: {input_hash}."
            )
            tokens_output = 68

        elif "replay" in sp_lower:
            content = (
                f"[MockLLM|replay|{input_hash}] "
                f"Replaying previous decision. "
                f"Input fingerprint: {input_hash}."
            )
            tokens_output = 30

        else:
            content = (
                f"[MockLLM|default|{input_hash}] "
                f"I have processed the input and generated this response. "
                f"Input fingerprint: {input_hash}."
            )
            tokens_output = 30

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            model_id=config.model_id,
            latency_ms=42,
            raw_response={
                "provider": "mock",
                "input_hash": input_hash,
                "deterministic": True,
            },
        )

    # ------------------------------------------------------------------
    # Row converters
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_config(row: dict[str, Any]) -> LLMConfig:
        return LLMConfig(
            config_id=row["config_id"],
            tenant_id=row["tenant_id"],
            provider=LLMProvider(row["provider"]),
            model_id=row["model_id"],
            api_key=row.get("api_key"),
            base_url=row.get("base_url"),
            default_temperature=row.get("temperature", 0.7),
            max_tokens=row.get("max_tokens", 4096),
            enabled=row.get("enabled", True),
            created_at=row["created_at"],
        )

    @staticmethod
    def config_to_dict(c: LLMConfig) -> dict[str, Any]:
        return {
            "config_id": c.config_id,
            "tenant_id": c.tenant_id,
            "provider": c.provider.value,
            "model_id": c.model_id,
            "api_key_set": c.api_key is not None,  # Never expose the key
            "base_url": c.base_url,
            "default_temperature": c.default_temperature,
            "max_tokens": c.max_tokens,
            "enabled": c.enabled,
            "created_at": c.created_at.isoformat(),
        }
