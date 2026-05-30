"""
GRAFOMEM MockLLMProvider — Deterministic fake LLM for testing.

This is NOT "skip the LLM." This is "replace the LLM with a deterministic
function" so that:
  1. The full orchestrator pipeline executes (governance → memory → inference → receipts → chain)
  2. Replay returns IDENTICAL (deterministic by construction)
  3. The function depends on the INPUT, not just the role, so replay actually
     tests input reconstruction (a mock keyed only by role would make replay
     vacuously pass even if the engine reconstructed the wrong input)
  4. One agent returns a tool_call to exercise Step 4 of execute_step

Architecture:
  - Registered as provider="mock" in the LLM Registry
  - The LLMRegistry.infer() dispatches to _infer_mock() when provider=MOCK
  - The response is: f(canonical(system_prompt, messages)) — a deterministic
    function of the full input, with role-keyed templates for readability
  - The researcher agent returns a grafomem_retrieve tool_call

Usage:
  registry.register_provider(tenant_id, "mock", "mock-model",
                              api_key="not-needed")
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def mock_infer(
    model_id: str,
    system_prompt: str,
    messages: list[dict[str, str]],
    tools: list[dict] | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    """Deterministic mock LLM inference.

    Returns a dict with the same shape as LLMResponse fields:
      content, tool_calls, tokens_input, tokens_output, model_id, latency_ms, raw_response

    The response is a DETERMINISTIC FUNCTION OF THE INPUT:
      - A canonical hash of (system_prompt + messages) is embedded in the output
      - Role detection determines the response template
      - If replay reconstructs the wrong input, the hash changes, and the
        replay test correctly reports DIVERGED instead of vacuously passing
    """
    # ── Canonical input hash ──────────────────────────────────
    # This is the critical piece: the output depends on the input,
    # so replay must reconstruct the exact input to get IDENTICAL.
    canonical = json.dumps({
        "system_prompt": system_prompt,
        "messages": messages,
    }, sort_keys=True, ensure_ascii=True)
    input_hash = hashlib.blake2b(canonical.encode(), digest_size=16).hexdigest()

    # ── Role detection (from system prompt) ───────────────────
    sp_lower = system_prompt.lower()
    last_user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_msg = m.get("content", "")
            break

    # ── Token counting (deterministic) ────────────────────────
    input_chars = len(system_prompt) + sum(len(m.get("content", "")) for m in messages)
    tokens_input = max(20, input_chars // 4)  # Rough approximation
    tool_calls: list[dict] = []

    # ── Generate response based on role ───────────────────────
    if "research" in sp_lower or "analyst" in sp_lower:
        content = (
            f"[MockLLM|researcher|{input_hash}] "
            f"Based on the retrieved compliance data, the key findings are: "
            f"1) GDPR Article 17 requires right to erasure with cryptographic proof. "
            f"2) EU AI Act Article 12 mandates logging of all AI decisions. "
            f"3) Rate limiting prevents abuse of agent resources. "
            f"Input fingerprint: {input_hash}. Analysis complete."
        )
        # Researcher returns a tool_call to exercise Step 4 of execute_step
        if tools:
            # Find a retrieve-like tool
            retrieve_tool = None
            for t in tools:
                if "retrieve" in t.get("name", "").lower():
                    retrieve_tool = t
                    break
            if retrieve_tool:
                tool_calls = [{
                    "name": retrieve_tool["name"],
                    "arguments": {"query": "GDPR compliance requirements"},
                }]
        tokens_output = 85

    elif "writ" in sp_lower or "draft" in sp_lower:
        content = (
            f"[MockLLM|writer|{input_hash}] "
            f"## Compliance Brief\n\n"
            f"Key regulatory requirements:\n"
            f"- **GDPR Art. 17**: Right to erasure must be implemented with proof.\n"
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

    else:
        content = (
            f"[MockLLM|default|{input_hash}] "
            f"I have processed the input and generated this response. "
            f"Input fingerprint: {input_hash}."
        )
        tokens_output = 30

    return {
        "content": content,
        "tool_calls": tool_calls,
        "tokens_input": tokens_input,
        "tokens_output": tokens_output,
        "model_id": model_id,
        "latency_ms": 42,  # Deterministic, not real
        "raw_response": {
            "provider": "mock",
            "input_hash": input_hash,
            "deterministic": True,
        },
    }
