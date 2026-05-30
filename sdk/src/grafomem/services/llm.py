"""LLM and tool registry service.

Register LLM providers (BYOM) and custom tools.

Usage::

    provider = client.llm.register_provider(
        model_id="gpt-4o-mini", provider="openai", api_key="sk-...",
    )
    tool = client.llm.register_tool(
        name="web_search", description="Search the web",
        parameters={"query": {"type": "string"}},
    )
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from grafomem.types import LLMProvider, ToolDefinition

if TYPE_CHECKING:
    from grafomem._http import HTTPTransport


class LLMService:
    """LLM provider and tool registry (BYOM)."""

    def __init__(self, http: HTTPTransport) -> None:
        self._http = http

    # ── Providers ─────────────────────────────────────────────────────

    def register_provider(
        self,
        model_id: str,
        provider: str,
        api_key: str,
        **kwargs: Any,
    ) -> LLMProvider:
        """Register an LLM provider.

        Args:
            model_id: Model identifier (e.g. ``"gpt-4o-mini"``).
            provider: Provider name (``"openai"``, ``"anthropic"``,
                ``"gemini"``).
            api_key: Provider API key.

        Returns:
            The registered :class:`LLMProvider`.
        """
        body: dict[str, Any] = {
            "model_id": model_id,
            "provider": provider,
            "api_key": api_key,
            **kwargs,
        }
        data = self._http.post("/v1/llm/providers", json=body)
        return LLMProvider.model_validate(data)

    def list_providers(self) -> list[LLMProvider]:
        """List all registered LLM providers."""
        data = self._http.get("/v1/llm/providers")
        items = data if isinstance(data, list) else data.get("providers", [])
        return [LLMProvider.model_validate(p) for p in items]

    def delete_provider(self, provider_id: str) -> None:
        """Remove a registered LLM provider."""
        self._http.delete(f"/v1/llm/providers/{provider_id}")

    # ── Tools ─────────────────────────────────────────────────────────

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ToolDefinition:
        """Register a custom tool.

        Args:
            name: Tool name (unique identifier).
            description: Human-readable description.
            parameters: JSON Schema for tool parameters.

        Returns:
            The registered :class:`ToolDefinition`.
        """
        body: dict[str, Any] = {
            "name": name,
            "description": description,
            "parameters": parameters or {},
            **kwargs,
        }
        data = self._http.post("/v1/llm/tools", json=body)
        return ToolDefinition.model_validate(data)

    def list_tools(self) -> list[ToolDefinition]:
        """List all registered tools."""
        data = self._http.get("/v1/llm/tools")
        items = data if isinstance(data, list) else data.get("tools", [])
        return [ToolDefinition.model_validate(t) for t in items]

    def delete_tool(self, name: str) -> None:
        """Remove a registered tool."""
        self._http.delete(f"/v1/llm/tools/{name}")

    def seed_builtins(self) -> list[ToolDefinition]:
        """Seed the built-in tool library.

        Returns:
            List of seeded :class:`ToolDefinition`.
        """
        data = self._http.post("/v1/llm/tools/seed-builtins")
        items = data if isinstance(data, list) else data.get("tools", [])
        return [ToolDefinition.model_validate(t) for t in items]
