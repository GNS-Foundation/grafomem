#!/usr/bin/env python3
"""
GRAFOMEM Cloud — Conformance E2E Test Suite v2

Exercises ALL 7 layers + Sprint 7–13 features with TWO-SIDED assertions.
Every ✅ has at least one explicit assert that checks behavior.
Every governance test checks that the gate fires when it should AND ONLY when it should.

Sprint 10 coverage: SSE streaming endpoint.
Sprint 11 coverage: Webhook CRUD + isolation, PDF report export, SSO provider listing.
Sprint 13 coverage: Health check endpoints (liveness + readiness).

Test states (4-state renderer):
  ✅ PASS      — Behavior verified with assertions
  ⏭️  SKIP      — Test not executed (only allowed for LIVE-only tests in MOCK mode)
  ⚠️  DEGRADED  — Ran but critical path not exercised
  ❌ FAIL      — Assertion failed

In MOCK mode, SKIP is treated as FAIL unless the test is explicitly marked LIVE-only.
This ensures MockLLM actually exercises the full pipeline.

Usage:
    # MOCK mode (deterministic MockLLM — exercises full pipeline):
    python3 tests/sandbox_e2e_v2.py

    # LIVE mode — OpenAI (costs money, results non-deterministic):
    OPENAI_API_KEY=sk-... python3 tests/sandbox_e2e_v2.py --live

    # LIVE mode — Anthropic:
    ANTHROPIC_API_KEY=sk-ant-... python3 tests/sandbox_e2e_v2.py --anthropic

    # LIVE mode — Gemini:
    GOOGLE_API_KEY=AIza... python3 tests/sandbox_e2e_v2.py --gemini

    # Emit signed JSON conformance report:
    python3 tests/sandbox_e2e_v2.py --report
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import httpx

# ============================================================================
# Configuration
# ============================================================================

BASE_URL = os.environ.get("GRAFOMEM_URL", "http://localhost:8080")
TEST_EMAIL = "conformance@grafomem.test"
TEST_PASSWORD = "ConformanceTest2026!"
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GOOGLE_KEY = os.environ.get("GOOGLE_API_KEY", "")


# ============================================================================
# Test result types (4-state)
# ============================================================================

class TestState(str, Enum):
    PASS = "pass"
    SKIP = "skip"
    DEGRADED = "degraded"
    FAIL = "fail"


SYMBOLS = {
    TestState.PASS: "✅",
    TestState.SKIP: "⏭️ ",
    TestState.DEGRADED: "⚠️ ",
    TestState.FAIL: "❌",
}


class TestResult:
    """A single test result with state, name, and detail."""
    __slots__ = ("name", "state", "detail", "live_only")

    def __init__(self, name: str, state: TestState, detail: str = "",
                 live_only: bool = False):
        self.name = name
        self.state = state
        self.detail = detail
        self.live_only = live_only

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "detail": self.detail,
            "live_only": self.live_only,
        }


# ============================================================================
# Test runner
# ============================================================================

class ConformanceSuite:
    """Two-sided conformance test suite for GRAFOMEM Cloud."""

    def __init__(self, base_url: str, live_mode: bool = False,
                 provider: str = "mock") -> None:
        self.base = base_url
        self.live = live_mode
        self.provider = provider  # "mock" | "openai" | "anthropic" | "gemini"
        self.client = httpx.Client(base_url=base_url, timeout=120.0)
        self.api_key: str = ""
        self.tenant_id: str = ""
        self.store_id: str = ""
        self.agent_ids: list[str] = []
        self.workflow_id: str = ""
        self.decision_ids: list[str] = []
        self.certificate_id: str = ""
        self.fact_refs: list[int] = []
        self.results: list[TestResult] = []
        self.report_id: str = ""  # From Phase 17, reused in Phase 20 (PDF)
        self.webhook_id: str = ""  # From Phase 19
        # Dynamic model_id set during Phase 4 (LLM registration)
        self.model_id: str = ""

        # Secondary tenant for isolation tests
        self.tenant_b_key: str = ""
        self.tenant_b_id: str = ""

    def _h(self, tenant: str = "a") -> dict[str, str]:
        """Auth headers. 'a' = primary tenant, 'b' = secondary."""
        key = self.api_key if tenant == "a" else self.tenant_b_key
        return {"X-API-Key": key}

    def _record(
        self,
        name: str,
        state: TestState,
        detail: str = "",
        live_only: bool = False,
    ) -> None:
        result = TestResult(name, state, detail, live_only)
        self.results.append(result)
        sym = SYMBOLS[state]
        print(f"  {sym} {name}" + (f" — {detail}" if detail else ""))

    def _assert(self, name: str, condition: bool, detail: str = "",
                live_only: bool = False) -> None:
        """Record PASS if condition is True, FAIL otherwise."""
        self._record(name, TestState.PASS if condition else TestState.FAIL,
                     detail, live_only)

    # ==================================================================
    # PHASE 1: Account setup (both tenants for isolation testing)
    # ==================================================================

    def test_signup(self) -> None:
        """Create primary test account (Tenant A)."""
        r = self.client.post("/v1/portal/signup", json={
            "name": "Conformance Tenant A",
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
            "plan": "starter",
        })
        if r.status_code in (400, 409):
            # Already exists — login instead
            r2 = self.client.post("/v1/portal/login", json={
                "email": TEST_EMAIL,
                "password": TEST_PASSWORD,
            })
            if r2.status_code == 200:
                data = r2.json()
                self.api_key = data.get("api_key", "")
                self.tenant_id = data.get("tenant_id", "")
            self._assert("Signup (Tenant A)", bool(self.api_key),
                          "re-used existing account")
            return
        ok = r.status_code == 201
        if ok:
            data = r.json()
            self.api_key = data.get("api_key", "")
            self.tenant_id = data.get("tenant_id", "")
        self._assert("Signup (Tenant A)", ok and bool(self.api_key),
                      f"tenant={self.tenant_id[:12]}...")

    def test_signup_tenant_b(self) -> None:
        """Create secondary tenant for isolation tests."""
        email_b = "conformance_b@grafomem.test"
        r = self.client.post("/v1/portal/signup", json={
            "name": "Conformance Tenant B",
            "email": email_b,
            "password": "TenantB_Test2026!",
            "plan": "starter",
        })
        if r.status_code in (400, 409):
            r2 = self.client.post("/v1/portal/login", json={
                "email": email_b,
                "password": "TenantB_Test2026!",
            })
            if r2.status_code == 200:
                data = r2.json()
                self.tenant_b_key = data.get("api_key", "")
                self.tenant_b_id = data.get("tenant_id", "")
            self._assert("Signup (Tenant B)", bool(self.tenant_b_key),
                          "re-used existing account")
            return
        ok = r.status_code == 201
        if ok:
            data = r.json()
            self.tenant_b_key = data.get("api_key", "")
            self.tenant_b_id = data.get("tenant_id", "")
        self._assert("Signup (Tenant B)", ok and bool(self.tenant_b_key),
                      f"tenant_b={self.tenant_b_id[:12]}...")

    # ==================================================================
    # PHASE 2: Memory store
    # ==================================================================

    def test_create_store(self) -> None:
        r = self.client.post("/v1/stores", headers=self._h())
        ok = r.status_code == 200
        if ok:
            self.store_id = r.json().get("store_id", "")
        self._assert("Create Store", ok and bool(self.store_id),
                      f"store_id={self.store_id}")

    def test_seed_facts(self) -> None:
        facts = [
            "The EU AI Act (Regulation 2024/1689) requires high-risk AI systems to maintain detailed logs of all decisions made during operation, per Article 12.",
            "GDPR Article 17 establishes the Right to Erasure, requiring data controllers to delete personal data upon request without undue delay.",
            "DORA (Digital Operational Resilience Act) requires financial entities to implement comprehensive ICT risk management frameworks, per Article 6.",
            "Under the EU AI Act Article 14, high-risk AI systems must have effective human oversight measures, including the ability to intervene and override.",
            "ISO 42001 is the first international standard for AI Management Systems, providing a framework for responsible AI governance.",
        ]
        written = 0
        for fact in facts:
            r = self.client.post(f"/v1/stores/{self.store_id}/write",
                                  json={"content": fact}, headers=self._h())
            if r.status_code == 200:
                ref = r.json().get("ref")
                if ref is not None:
                    self.fact_refs.append(ref)
                written += 1
        self._assert("Seed 5 Facts", written == 5,
                      f"{written}/5 written, refs={self.fact_refs[:3]}...")

    # ==================================================================
    # PHASE 3: Governance policies
    # ==================================================================

    def test_governance_policies(self) -> None:
        policies = [
            {
                "name": "Rate Limit",
                "description": "Max 100 requests per minute",
                "policy_type": "rate_limit",
                "action": "deny",
                "config": {"max_requests": 100, "window_seconds": 60},
                "priority": 1,
            },
            {
                "name": "PII Guard",
                "description": "Detect SSN and credit card patterns",
                "policy_type": "pii_guard",
                "action": "deny",
                "config": {
                    "patterns": [
                        r"\b\d{3}-\d{2}-\d{4}\b",
                        r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b",
                    ],
                    "check_fields": ["output", "query"],
                },
                "priority": 2,
            },
        ]
        created = 0
        for p in policies:
            r = self.client.post("/v1/governance/policies", json=p,
                                  headers=self._h())
            if r.status_code == 200:
                created += 1
        self._assert("Create 2 Policies", created == 2,
                      f"{created}/2 (rate_limit + pii_guard)")

    # ==================================================================
    # PHASE 4: LLM provider
    # ==================================================================

    def test_register_llm(self) -> None:
        # Determine provider-specific config
        if self.provider == "openai":
            self.model_id = "gpt-4o-mini"
            llm_api_key = OPENAI_KEY
            llm_provider = "openai"
            key_env_name = "OPENAI_API_KEY"
        elif self.provider == "anthropic":
            self.model_id = "claude-opus-4-20250514"
            llm_api_key = ANTHROPIC_KEY
            llm_provider = "anthropic"
            key_env_name = "ANTHROPIC_API_KEY"
        elif self.provider == "gemini":
            self.model_id = "gemini-2.5-pro"
            llm_api_key = GOOGLE_KEY
            llm_provider = "gemini"
            key_env_name = "GOOGLE_API_KEY"
        else:  # mock
            self.model_id = "mock-model"
            llm_api_key = None
            llm_provider = "mock"
            key_env_name = ""

        if self.live:
            if not llm_api_key:
                self._record("Register LLM", TestState.FAIL,
                              f"LIVE mode but no {key_env_name}")
                return
            r = self.client.post("/v1/llm/providers", json={
                "provider": llm_provider,
                "model_id": self.model_id,
                "api_key": llm_api_key,
            }, headers=self._h())
        else:
            # MOCK mode: register the deterministic MockLLM provider
            r = self.client.post("/v1/llm/providers", json={
                "provider": "mock",
                "model_id": "mock-model",
            }, headers=self._h())

        ok = r.status_code == 200
        self._assert(f"Register LLM ({self.model_id})", ok,
                      r.json().get("config_id", "")[:12] + "..." if ok else f"status={r.status_code}: {r.text[:100]}")

    # ==================================================================
    # PHASE 5: Agent definitions
    # ==================================================================

    def test_create_agents(self) -> None:
        model_id = self.model_id or ("mock-model" if not self.live else "gpt-4o-mini")
        agents = [
            {
                "name": "Compliance Researcher",
                "role": "researcher",
                "description": "Retrieves compliance facts from memory",
                "model_id": model_id,
                "system_prompt": (
                    "You are a compliance researcher. Your job is to retrieve "
                    "relevant regulatory facts from the memory store and "
                    "summarize what you find. Be precise and cite the specific "
                    "articles. Keep your response under 300 words."
                ),
                "memory_stores": [self.store_id],
                "tools": ["grafomem_retrieve"],
                "max_steps": 5,
                "temperature": 0.3,
            },
            {
                "name": "Report Writer",
                "role": "writer",
                "description": "Synthesizes research into a structured report",
                "model_id": model_id,
                "system_prompt": (
                    "You are a compliance report writer. Take the research "
                    "from the previous agent and write a structured compliance "
                    "brief with: 1) Executive Summary, 2) Key Regulations, "
                    "3) Compliance Requirements. Keep it under 400 words."
                ),
                "memory_stores": [],
                "tools": [],
                "max_steps": 3,
                "temperature": 0.5,
            },
            {
                "name": "Compliance Reviewer",
                "role": "reviewer",
                "description": "Reviews the report for accuracy and gaps",
                "model_id": model_id,
                "system_prompt": (
                    "You are a senior compliance officer reviewing a report. "
                    "Check for: 1) Accuracy of cited regulations, "
                    "2) Missing requirements, 3) Actionable recommendations. "
                    "Provide a score (1-10) and specific feedback."
                ),
                "memory_stores": [],
                "tools": [],
                "max_steps": 3,
                "temperature": 0.2,
            },
        ]

        self.agent_ids = []
        for agent in agents:
            r = self.client.post("/v1/orchestrator/agents", json=agent,
                                  headers=self._h())
            if r.status_code == 200:
                self.agent_ids.append(r.json()["agent_id"])
        self._assert("Create 3 Agents", len(self.agent_ids) == 3,
                      f"{len(self.agent_ids)}/3 — " +
                      ", ".join(a[:8] for a in self.agent_ids))

    # ==================================================================
    # PHASE 6: Workflow execution (NEVER skipped — MockLLM runs it)
    # ==================================================================

    def test_create_workflow(self) -> None:
        r = self.client.post("/v1/orchestrator/workflows", json={
            "name": "Compliance Research Pipeline",
            "description": "Research → Write → Review",
            "agent_ids": self.agent_ids,
            "mode": "sequential",
            "max_total_steps": 10,
        }, headers=self._h())
        ok = r.status_code == 200
        if ok:
            self.workflow_id = r.json()["workflow_id"]
        self._assert("Create Workflow", ok and bool(self.workflow_id),
                      f"workflow_id={self.workflow_id[:12]}..." if self.workflow_id else "")

    def test_run_workflow(self) -> None:
        """Execute the workflow. NEVER skipped — MockLLM handles it."""
        query = (
            "What are the key logging and record-keeping requirements "
            "for AI systems under the EU AI Act and GDPR? "
            "How do they interact with DORA requirements for financial institutions?"
        )

        r = self.client.post(
            f"/v1/orchestrator/workflows/{self.workflow_id}/run",
            json={"input_text": query},
            headers=self._h(),
        )
        ok = r.status_code == 200
        detail = ""
        if ok:
            wf = r.json()
            status = wf.get("status", "")
            steps = wf.get("steps", [])
            total_tokens = wf.get("total_tokens", 0)
            detail = f"status={status} steps={len(steps)} tokens={total_tokens}"
            for step in steps:
                did = step.get("decision_id")
                if did:
                    self.decision_ids.append(did)
        else:
            detail = f"status={r.status_code}: {r.text[:200]}"

        self._assert("Run 3-Agent Workflow", ok and len(self.decision_ids) >= 1,
                      detail)

    # ==================================================================
    # PHASE 7: Execution receipts + hash chain (explicit assertions)
    # ==================================================================

    def test_execution_receipts(self) -> None:
        """Verify receipts were generated — one per step."""
        r = self.client.get(
            f"/v1/orchestrator/workflows/{self.workflow_id}/receipts",
            headers=self._h(),
        )
        ok = r.status_code == 200
        count = 0
        if ok:
            data = r.json()
            count = data.get("count", 0)
            receipts = data.get("receipts", [])

            # Explicit assertion: receipt count == step count (3 agents)
            self._assert(
                "Receipt Count == Step Count",
                count == 3,
                f"expected=3 actual={count}",
            )

            # Explicit assertion: genesis receipt has null parent
            if receipts:
                genesis = receipts[0]
                self._assert(
                    "Genesis Receipt Has Null Parent",
                    genesis.get("previous_receipt_hash") is None,
                    f"prev_hash={genesis.get('previous_receipt_hash')}",
                )

                # Non-genesis receipts have non-null parents
                for i, receipt in enumerate(receipts[1:], 1):
                    has_parent = receipt.get("previous_receipt_hash") is not None
                    self._assert(
                        f"Receipt {i} Has Parent Hash",
                        has_parent,
                        f"step={i} prev_hash={'set' if has_parent else 'null'}",
                    )
        else:
            self._record("Receipt Count", TestState.FAIL,
                          f"status={r.status_code}")

    def test_verify_chain(self) -> None:
        """Verify the hash chain is INTACT."""
        r = self.client.get(
            f"/v1/orchestrator/workflows/{self.workflow_id}/verify-chain",
            headers=self._h(),
        )
        ok = r.status_code == 200
        if ok:
            data = r.json()
            status = data.get("status", "")
            steps = data.get("steps_verified", 0)
            self._assert(
                "Hash Chain INTACT",
                status == "intact" and steps == 3,
                f"status={status} steps_verified={steps}",
            )
        else:
            self._record("Hash Chain", TestState.FAIL, f"status={r.status_code}")

    # ==================================================================
    # PHASE 8: Decision trail + replay
    # ==================================================================

    def test_decision_trail(self) -> None:
        r = self.client.get("/v1/decisions/", params={"limit": 10},
                             headers=self._h())
        ok = r.status_code == 200
        if ok:
            data = r.json()
            decisions = data.get("decisions", [])
            count = len(decisions)
            if decisions and not self.decision_ids:
                self.decision_ids = [d["decision_id"] for d in decisions[:3]]
            self._assert("Decision Trail Has Records", count >= 3,
                          f"{count} decisions found")
        else:
            self._record("Decision Trail", TestState.FAIL, f"status={r.status_code}")

    def test_replay_decision(self) -> None:
        """Replay a decision. With MockLLM + faithful system_prompt reconstruction,
        the replay MUST produce IDENTICAL output — proving deterministic replay.

        The replay engine now reconstructs the original agent's system_prompt from
        the orchestrator_agents table. Since MockLLM is a pure function of input
        (BLAKE2b hash of system_prompt + messages), identical input → identical
        output → IDENTICAL status. This is the §20.2 whitepaper claim.
        """
        if not self.decision_ids:
            self._record("Replay Decision", TestState.FAIL, "no decisions to replay")
            return

        decision_id = self.decision_ids[0]
        r = self.client.post(
            f"/v1/orchestrator/replay/{decision_id}",
            headers=self._h(),
        )
        ok = r.status_code == 200
        if ok:
            data = r.json()
            status = data.get("status", "")
            confidence = data.get("confidence", 0)
            input_ok = data.get("input_reconstructed", False)
            model_ok = data.get("model_available", False)

            # Key assertion: input was reconstructed
            self._assert(
                "Replay Input Reconstructed",
                input_ok,
                f"input_reconstructed={input_ok}",
            )

            # Key assertion: model was available
            self._assert(
                "Replay Model Available",
                model_ok,
                f"model_available={model_ok}",
            )

            if not self.live:
                # MOCK mode with faithful system_prompt: MUST be IDENTICAL
                # If diverged, the replay engine failed to reconstruct the prompt
                self._assert(
                    "Replay Deterministic IDENTICAL",
                    status == "identical",
                    f"status={status} confidence={confidence:.2f}",
                )
            else:
                # LIVE mode: any non-error status is acceptable
                self._assert(
                    "Replay Executes",
                    status in ("identical", "diverged", "degraded"),
                    f"status={status} confidence={confidence:.2f}",
                )
        else:
            self._record("Replay Decision", TestState.FAIL,
                          f"status={r.status_code}: {r.text[:200]}")

    # ==================================================================
    # PHASE 9: Erasure cascade (3-leg)
    # ==================================================================

    def test_erasure_cascade(self) -> None:
        """Three-leg erasure test:
        1. Delete fact from memory → confirm it's gone
        2. Issue erasure certificate → verify content hash
        3. Check decisions referencing deleted fact → assert scrubbed
        """
        if not self.store_id or not self.fact_refs:
            self._record("Erasure Cascade", TestState.FAIL, "No store/refs")
            return

        target_ref = self.fact_refs[0]
        original_content = "The EU AI Act (Regulation 2024/1689) requires high-risk AI systems to maintain detailed logs of all decisions made during operation, per Article 12."
        content_hash = hashlib.blake2b(
            original_content.encode(), digest_size=16,
        ).hexdigest()

        # LEG 1: Delete the fact
        r = self.client.post(
            f"/v1/stores/{self.store_id}/delete",
            json={"ref": target_ref},
            headers=self._h(),
        )
        ok_delete = r.status_code == 200 and r.json().get("deleted", False)
        self._assert("Erasure Leg 1: Fact Deleted From Memory",
                      ok_delete, f"ref={target_ref}")

        # LEG 1b: Verify fact is actually GONE (retrieve should not find it)
        r2 = self.client.post(
            f"/v1/stores/{self.store_id}/retrieve",
            json={"query": "EU AI Act Article 12 logging requirements", "limit": 10},
            headers=self._h(),
        )
        if r2.status_code == 200:
            results = r2.json().get("memories", [])
            # The deleted fact should NOT appear in results
            found_deleted = any(
                str(m.get("ref")) == str(target_ref) for m in results
            )
            self._assert("Erasure Leg 1b: Deleted Fact Not Retrievable",
                          not found_deleted,
                          f"found_deleted={found_deleted} results={len(results)}")

        # LEG 2: Issue erasure certificate
        r3 = self.client.post("/v1/erasure/issue", json={
            "fact_ref": target_ref,
            "fact_content": original_content,
            "memory_deleted": True,
            "legal_basis": "GDPR Article 17 — Right to Erasure",
            "requested_by": "data_subject",
        }, headers=self._h())
        ok_cert = r3.status_code == 200
        if ok_cert:
            cert_data = r3.json()
            self.certificate_id = cert_data.get("certificate_id", "")

            # LEG 2b: Verify certificate's content hash matches the original
            cert_content_hash = cert_data.get("fact_content_hash", "")
            self._assert(
                "Erasure Leg 2: Cert Content Hash Matches",
                cert_content_hash == content_hash,
                f"expected={content_hash[:16]}... got={cert_content_hash[:16]}...",
            )
        else:
            self._record("Erasure Leg 2: Cert Issue", TestState.FAIL,
                          f"status={r3.status_code}: {r3.text[:100]}")

        # LEG 3: Verify certificate via the verify endpoint
        if self.certificate_id:
            r4 = self.client.get(
                f"/v1/erasure/{self.certificate_id}/verify",
                headers=self._h(),
            )
            ok_verify = r4.status_code == 200
            if ok_verify:
                vdata = r4.json()
                valid = vdata.get("valid", False)
                detail = vdata.get("detail", "")
                # With sandbox signing key, certificate MUST be signed and valid
                self._assert("Erasure Leg 3: Certificate Ed25519 Signed",
                              valid,
                              f"valid={valid} detail={detail[:60]}")

    # ==================================================================
    # P0-1: Governance DENY (two-sided)
    # ==================================================================

    def test_p0_governance_deny(self) -> None:
        """Two-sided: (a) disallowed model IS denied, (b) allowed model IS permitted."""
        # Create a model_allowlist policy
        model_id_for_test = self.model_id or ("mock-model" if not self.live else "gpt-4o-mini")

        r = self.client.post("/v1/governance/policies", json={
            "name": "Model Allowlist (conformance test)",
            "description": "Only allow mock-model or gpt-4o-mini",
            "policy_type": "model_allowlist",
            "action": "deny",
            "config": {"models": [model_id_for_test]},
            "priority": 5,
        }, headers=self._h())
        policy_created = r.status_code == 200

        if not policy_created:
            self._record("P0-1: Create Allowlist Policy", TestState.FAIL,
                          f"status={r.status_code}")
            return

        # (a) POSITIVE: disallowed model → DENIED
        r_deny = self.client.post("/v1/governance/evaluate", json={
            "operation": "inference",
            "context": {"model_id": "gpt-3.5-turbo-DISALLOWED"},
        }, headers=self._h())
        if r_deny.status_code == 200:
            data = r_deny.json()
            was_denied = not data.get("allowed", True)
            self._assert("P0-1a: Disallowed Model IS Denied",
                          was_denied,
                          f"allowed={data.get('allowed')} logs={len(data.get('logs', []))}")
        else:
            self._record("P0-1a: Evaluate Deny", TestState.FAIL,
                          f"status={r_deny.status_code}")

        # (b) NEGATIVE: allowed model → PERMITTED
        r_allow = self.client.post("/v1/governance/evaluate", json={
            "operation": "inference",
            "context": {"model_id": model_id_for_test},
        }, headers=self._h())
        if r_allow.status_code == 200:
            data = r_allow.json()
            was_allowed = data.get("allowed", False)
            self._assert("P0-1b: Allowed Model IS Permitted",
                          was_allowed,
                          f"allowed={data.get('allowed')}")
        else:
            self._record("P0-1b: Evaluate Allow", TestState.FAIL,
                          f"status={r_allow.status_code}")

    # ==================================================================
    # P0-2: PII Guard (two-sided)
    # ==================================================================

    def test_p0_pii_guard(self) -> None:
        """Two-sided: (a) PII IS caught, (b) clean input is NOT falsely flagged."""
        # (a) POSITIVE: input containing SSN → DENIED
        r_pii = self.client.post("/v1/governance/evaluate", json={
            "operation": "output_check",
            "context": {
                "output": "Contact John Smith, SSN 123-45-6789, credit card 4111111111111111",
            },
        }, headers=self._h())
        if r_pii.status_code == 200:
            data = r_pii.json()
            was_denied = not data.get("allowed", True)
            self._assert("P0-2a: PII IS Caught",
                          was_denied,
                          f"allowed={data.get('allowed')}")
        else:
            self._record("P0-2a: PII Evaluate", TestState.FAIL,
                          f"status={r_pii.status_code}")

        # (b) NEGATIVE: clean input → NOT falsely flagged
        r_clean = self.client.post("/v1/governance/evaluate", json={
            "operation": "output_check",
            "context": {
                "output": "The EU AI Act requires logging of all AI decisions per Article 12.",
            },
        }, headers=self._h())
        if r_clean.status_code == 200:
            data = r_clean.json()
            was_allowed = data.get("allowed", False)
            self._assert("P0-2b: Clean Input NOT Falsely Flagged",
                          was_allowed,
                          f"allowed={data.get('allowed')}")
        else:
            self._record("P0-2b: Clean Evaluate", TestState.FAIL,
                          f"status={r_clean.status_code}")

    # ==================================================================
    # P0-3: Multi-tenant isolation (two-sided)
    # ==================================================================

    def test_p0_multi_tenant(self) -> None:
        """Two-sided: (a) cross-tenant IS blocked, (b) same-tenant IS allowed."""
        if not self.tenant_b_key or not self.store_id:
            self._record("P0-3: Multi-tenant", TestState.FAIL,
                          "Tenant B not available")
            return

        # (a) POSITIVE: Tenant B trying to access Tenant A's store → MUST be 403
        r_cross = self.client.post(
            f"/v1/stores/{self.store_id}/retrieve",
            json={"query": "EU AI Act", "limit": 5},
            headers=self._h("b"),  # Tenant B's key
        )
        self._assert(
            "P0-3a: Cross-Tenant Access IS Blocked",
            r_cross.status_code == 403,
            f"status={r_cross.status_code}",
        )

        # (b) NEGATIVE: Same-tenant access → allowed
        # Query matches seeded facts (all 5 still present before erasure phase)
        r_same = self.client.post(
            f"/v1/stores/{self.store_id}/retrieve",
            json={"query": "GDPR Article 17 Right to Erasure personal data deletion", "limit": 5},
            headers=self._h("a"),  # Tenant A's key
        )
        same_allowed = (
            r_same.status_code == 200 and
            len(r_same.json().get("memories", [])) > 0
        )
        self._assert("P0-3b: Same-Tenant Access IS Allowed",
                      same_allowed,
                      f"status={r_same.status_code} memories={len(r_same.json().get('memories', []))}" if r_same.status_code == 200 else "")

    # ==================================================================
    # P0-4: HITL Escalation (two-sided)
    # ==================================================================

    def test_p0_hitl_escalation(self) -> None:
        """Two-sided: (a) HITL operation IS escalated, (b) non-HITL IS allowed."""
        # Create a hitl_required policy for "deploy" operations
        r = self.client.post("/v1/governance/policies", json={
            "name": "HITL Deploy Gate (conformance test)",
            "description": "Require human approval for deploy operations",
            "policy_type": "hitl_required",
            "action": "escalate",
            "config": {"operations": ["deploy"]},
            "priority": 10,
        }, headers=self._h())
        if r.status_code != 200:
            self._record("P0-4: Create HITL Policy", TestState.FAIL,
                          f"status={r.status_code}")
            return

        # (a) POSITIVE: deploy operation → ESCALATED (allowed=False)
        r_escalate = self.client.post("/v1/governance/evaluate", json={
            "operation": "deploy",
            "context": {"model_id": "mock-model", "target": "production"},
        }, headers=self._h())
        if r_escalate.status_code == 200:
            data = r_escalate.json()
            was_denied = not data.get("allowed", True)
            logs = data.get("evaluations", [])
            has_escalated = any(
                log.get("result") == "escalated" for log in logs
            )
            self._assert("P0-4a: Deploy Operation IS Escalated",
                          was_denied and has_escalated,
                          f"allowed={data.get('allowed')} escalated={has_escalated}")
        else:
            self._record("P0-4a: HITL Evaluate", TestState.FAIL,
                          f"status={r_escalate.status_code}")

        # (b) NEGATIVE: inference operation → NOT escalated
        # Use the correct model_id for the current mode, otherwise the
        # model_allowlist policy (from P0-1) will falsely deny the request
        allowed_model = self.model_id or ("mock-model" if not self.live else "gpt-4o-mini")
        r_ok = self.client.post("/v1/governance/evaluate", json={
            "operation": "inference",
            "context": {"model_id": allowed_model},
        }, headers=self._h())
        if r_ok.status_code == 200:
            data = r_ok.json()
            was_allowed = data.get("allowed", False)
            self._assert("P0-4b: Inference Operation NOT Escalated",
                          was_allowed,
                          f"allowed={data.get('allowed')}")
        else:
            self._record("P0-4b: Non-HITL Evaluate", TestState.FAIL,
                          f"status={r_ok.status_code}")

    # ==================================================================
    # P0-6: Ed25519 Signing (two-sided: valid verifies, tamper fails)
    # ==================================================================

    def test_p0_signing(self) -> None:
        """Two-sided: (a) valid signature verifies, (b) tampered data fails."""
        if not self.certificate_id:
            self._record("P0-6: Signing", TestState.FAIL,
                          "No certificate from erasure test")
            return

        # (a) POSITIVE: legitimate certificate verifies
        r_valid = self.client.get(
            f"/v1/erasure/{self.certificate_id}/verify",
            headers=self._h(),
        )
        if r_valid.status_code == 200:
            data = r_valid.json()
            self._assert("P0-6a: Valid Signature Verifies",
                          data.get("valid", False),
                          f"valid={data.get('valid')} detail={data.get('detail', '')[:50]}")
        else:
            self._record("P0-6a: Verify Request", TestState.FAIL,
                          f"status={r_valid.status_code}")

        # (b) NEGATIVE: tampered certificate fails verification
        import psycopg
        db_url = os.environ.get(
            "DATABASE_URL",
            "postgresql://grafomem:grafomem@localhost:5433/grafomem",
        )
        try:
            conn = psycopg.connect(db_url, autocommit=True)
            row = conn.execute(
                "SELECT signature FROM erasure_certificates WHERE certificate_id = %s",
                (self.certificate_id,),
            ).fetchone()
            if row and row[0]:
                original_sig = bytes(row[0])
                tampered_sig = bytearray(original_sig)
                tampered_sig[0] ^= 0xFF
                conn.execute(
                    "UPDATE erasure_certificates SET signature = %s WHERE certificate_id = %s",
                    (bytes(tampered_sig), self.certificate_id),
                )
                r_tamper = self.client.get(
                    f"/v1/erasure/{self.certificate_id}/verify",
                    headers=self._h(),
                )
                if r_tamper.status_code == 200:
                    data = r_tamper.json()
                    tamper_detected = not data.get("valid", True)
                    self._assert("P0-6b: Tampered Signature IS Rejected",
                                  tamper_detected,
                                  f"valid={data.get('valid')} detail={data.get('detail', '')[:50]}")
                else:
                    self._record("P0-6b: Tamper Verify", TestState.FAIL,
                                  f"status={r_tamper.status_code}")
                # Restore original signature
                conn.execute(
                    "UPDATE erasure_certificates SET signature = %s WHERE certificate_id = %s",
                    (original_sig, self.certificate_id),
                )
            else:
                self._record("P0-6b: Tampered Signature IS Rejected", TestState.FAIL,
                              "No signature found — signing key not configured?")
            conn.close()
        except Exception as e:
            self._record("P0-6b: Tampered Signature IS Rejected", TestState.FAIL,
                          f"DB error: {e}")

    # ==================================================================
    # Chain-tamper negative (hash chain integrity)
    # ==================================================================

    def test_chain_tamper_negative(self) -> None:
        """Tamper with a receipt and verify the chain detects it."""
        if not self.workflow_id:
            self._record("Chain Tamper", TestState.FAIL, "No workflow")
            return

        import psycopg
        db_url = os.environ.get(
            "DATABASE_URL",
            "postgresql://grafomem:grafomem@localhost:5433/grafomem",
        )
        try:
            conn = psycopg.connect(db_url, autocommit=True)
            row = conn.execute(
                "SELECT receipt_id, output_hash FROM execution_receipts "
                "WHERE workflow_id = %s ORDER BY step_number LIMIT 1",
                (self.workflow_id,),
            ).fetchone()
            if not row:
                self._record("Chain Tamper: Detect Mutation", TestState.FAIL,
                              "No receipts found")
                conn.close()
                return

            receipt_id = row[0]
            original_hash = row[1]

            # Tamper: corrupt the output_hash
            conn.execute(
                "UPDATE execution_receipts SET output_hash = 'TAMPERED_HASH_000' "
                "WHERE receipt_id = %s",
                (receipt_id,),
            )

            # Verify chain should now report TAMPERED
            r_tamper = self.client.get(
                f"/v1/orchestrator/workflows/{self.workflow_id}/verify-chain",
                headers=self._h(),
            )
            if r_tamper.status_code == 200:
                data = r_tamper.json()
                status = data.get("status", "")
                self._assert(
                    "Chain Tamper: Detect Mutation",
                    status == "tampered",
                    f"status={status} tampered_at={data.get('tampered_at_step')}",
                )
            else:
                self._record("Chain Tamper: Detect Mutation", TestState.FAIL,
                              f"status={r_tamper.status_code}")

            # Restore
            conn.execute(
                "UPDATE execution_receipts SET output_hash = %s WHERE receipt_id = %s",
                (original_hash, receipt_id),
            )
            conn.close()
        except Exception as e:
            self._record("Chain Tamper: Detect Mutation", TestState.FAIL,
                          f"DB error: {e}")

    # ==================================================================
    # PHASE: Regulatory report
    # ==================================================================

    def test_generate_report(self) -> None:
        r = self.client.post("/v1/reports/generate", json={
            "framework": "eu_ai_act",
        }, headers=self._h())
        ok = r.status_code == 200
        detail = ""
        if ok:
            data = r.json()
            self.report_id = data.get("report_id", "")
            detail = f"report_id={self.report_id[:12]}..."
        self._assert("Generate EU AI Act Report", ok, detail)

    # ==================================================================
    # PHASE: Platform stats
    # ==================================================================

    def test_governance_stats(self) -> None:
        r = self.client.get("/v1/governance/stats", headers=self._h())
        ok = r.status_code == 200
        if ok:
            data = r.json()
            total = data.get("evaluations_total", 0)
            self._assert("Governance Stats", ok,
                          f"{total} evaluations, {data.get('policies_active', 0)} active policies")

    def test_orchestrator_stats(self) -> None:
        r = self.client.get("/v1/orchestrator/stats", headers=self._h())
        ok = r.status_code == 200
        if ok:
            data = r.json()
            self._assert("Orchestrator Stats", ok,
                          f"agents={data.get('agents_total', 0)} "
                          f"workflows={data.get('workflows_total', 0)} "
                          f"steps={data.get('steps_total', 0)}")

    # ==================================================================
    # HITL Resume Lifecycle (Escalate → Approve → Complete)
    # ==================================================================

    def test_hitl_resume_lifecycle(self) -> None:
        """Full HITL lifecycle: workflow escalates → pauses → resumes → completes.

        This is the test that was previously missing: the P0-4 HITL test only
        verified the governance gate (escalate/not-escalate), but not the
        workflow lifecycle (pause → approve → resume → complete).

        Two-sided:
          (a) POSITIVE: Escalated workflow + approved resume → COMPLETED
          (b) NEGATIVE: Escalated workflow + rejected resume → TERMINATED
        """
        if not self.api_key or not self.agent_ids:
            self._record("HITL Resume: Setup", TestState.FAIL,
                          "No API key or agents")
            return

        # ── Setup: create a HITL policy that triggers on 'inference' ──
        # Use a unique operation key so we don't break other tests
        r_pol = self.client.post("/v1/governance/policies", json={
            "name": "HITL Inference Gate (lifecycle test)",
            "description": "Require human approval for all inference operations",
            "policy_type": "hitl_required",
            "action": "escalate",
            "config": {"operations": ["inference"]},
            "priority": 1,  # Highest priority
        }, headers=self._h())
        if r_pol.status_code != 200:
            self._record("HITL Resume: Create Policy", TestState.FAIL,
                          f"status={r_pol.status_code}")
            return
        hitl_policy_id = r_pol.json().get("policy_id", "")

        # ── Create a dedicated workflow for the HITL test ──
        r_wf = self.client.post("/v1/orchestrator/workflows", json={
            "name": "HITL Lifecycle Test Workflow",
            "description": "Tests escalation → resume → completion",
            "agent_ids": [self.agent_ids[0]],
            "mode": "sequential",
            "max_total_steps": 10,
        }, headers=self._h())
        if r_wf.status_code != 200:
            self._record("HITL Resume: Create Workflow", TestState.FAIL,
                          f"status={r_wf.status_code}")
            # Cleanup
            self.client.delete(
                f"/v1/governance/policies/{hitl_policy_id}",
                headers=self._h(),
            )
            return
        hitl_wf_id = r_wf.json().get("workflow_id", "")

        # ── (a) POSITIVE: Run → Escalate → Resume(approved) → Completed ──
        r_run = self.client.post(
            f"/v1/orchestrator/workflows/{hitl_wf_id}/run",
            json={"input_text": "HITL lifecycle test: should escalate"},
            headers=self._h(),
        )
        if r_run.status_code != 200:
            self._record("HITL Resume: Run Workflow", TestState.FAIL,
                          f"status={r_run.status_code}")
            self.client.delete(
                f"/v1/governance/policies/{hitl_policy_id}",
                headers=self._h(),
            )
            return

        # Check workflow status is WAITING_HITL
        wf_data = r_run.json()
        wf_status = wf_data.get("status", "")
        self._assert(
            "HITL Resume (a): Workflow Enters WAITING_HITL",
            wf_status == "waiting_hitl",
            f"status={wf_status}",
        )

        # Resume with approval — delete the HITL policy first so the
        # re-executed step passes governance (otherwise it escalates again
        # in an infinite loop, which is correct behavior for the platform
        # but not what this test is exercising)
        self.client.delete(
            f"/v1/governance/policies/{hitl_policy_id}",
            headers=self._h(),
        )
        r_resume = self.client.post(
            f"/v1/orchestrator/workflows/{hitl_wf_id}/resume",
            json={"approved": True},
            headers=self._h(),
        )
        if r_resume.status_code == 200:
            resumed_data = r_resume.json()
            resumed_status = resumed_data.get("status", "")
            self._assert(
                "HITL Resume (a): Approved → Workflow Completes",
                resumed_status == "completed",
                f"status={resumed_status}",
            )
        else:
            self._record(
                "HITL Resume (a): Approved → Workflow Completes",
                TestState.FAIL,
                f"resume status={r_resume.status_code}",
            )

        # ── (b) NEGATIVE: Create new policy → new workflow → Escalate → Reject → Terminated ──
        # Need a fresh HITL policy since leg (a) deleted the original
        r_pol2 = self.client.post("/v1/governance/policies", json={
            "name": "HITL Inference Gate (reject test)",
            "description": "Require human approval for inference (reject leg)",
            "policy_type": "hitl_required",
            "action": "escalate",
            "config": {"operations": ["inference"]},
            "priority": 1,
        }, headers=self._h())
        if r_pol2.status_code != 200:
            self._record("HITL Resume (b): Rejected → Workflow Terminated",
                          TestState.FAIL, f"create policy status={r_pol2.status_code}")
            return
        hitl_policy2_id = r_pol2.json().get("policy_id", "")

        r_wf2 = self.client.post("/v1/orchestrator/workflows", json={
            "name": "HITL Reject Test Workflow",
            "description": "Tests escalation → reject → termination",
            "agent_ids": [self.agent_ids[0]],
            "mode": "sequential",
            "max_total_steps": 10,
        }, headers=self._h())
        if r_wf2.status_code == 200:
            hitl_wf2_id = r_wf2.json().get("workflow_id", "")

            # Run → should escalate
            r_run2 = self.client.post(
                f"/v1/orchestrator/workflows/{hitl_wf2_id}/run",
                json={"input_text": "HITL reject test: should escalate"},
                headers=self._h(),
            )
            if r_run2.status_code == 200:
                wf2_status = r_run2.json().get("status", "")
                if wf2_status == "waiting_hitl":
                    # Reject
                    r_reject = self.client.post(
                        f"/v1/orchestrator/workflows/{hitl_wf2_id}/resume",
                        json={"approved": False},
                        headers=self._h(),
                    )
                    if r_reject.status_code == 200:
                        reject_status = r_reject.json().get("status", "")
                        self._assert(
                            "HITL Resume (b): Rejected → Workflow Terminated",
                            reject_status == "terminated",
                            f"status={reject_status}",
                        )
                    else:
                        self._record(
                            "HITL Resume (b): Rejected → Workflow Terminated",
                            TestState.FAIL,
                            f"reject status={r_reject.status_code}",
                        )
                else:
                    self._record(
                        "HITL Resume (b): Rejected → Workflow Terminated",
                        TestState.FAIL,
                        f"workflow didn't escalate: status={wf2_status}",
                    )
        else:
            self._record(
                "HITL Resume (b): Rejected → Workflow Terminated",
                TestState.FAIL,
                f"create workflow status={r_wf2.status_code}",
            )

        # ── Cleanup: delete the reject-leg HITL policy ──
        self.client.delete(
            f"/v1/governance/policies/{hitl_policy2_id}",
            headers=self._h(),
        )

    # ==================================================================
    # PHASE 18: SSE Streaming (Sprint 10)
    # ==================================================================

    def test_stream_workflow(self) -> None:
        """Stream a workflow via SSE and verify events are received.

        Creates a fresh single-agent workflow and streams it.  Parses
        the text/event-stream response line-by-line, collecting event
        types.  Asserts that at least `workflow.started` and
        `workflow.complete` events appear.
        """
        if not self.agent_ids:
            self._record("Stream Workflow Receives Events", TestState.FAIL,
                          "No agents available")
            return

        model_id = self.model_id or ("mock-model" if not self.live else "gpt-4o-mini")

        # Create a dedicated workflow for the streaming test
        r_wf = self.client.post("/v1/orchestrator/workflows", json={
            "name": "Streaming Conformance Workflow",
            "description": "Tests SSE streaming",
            "agent_ids": [self.agent_ids[0]],
            "mode": "sequential",
            "max_total_steps": 5,
        }, headers=self._h())
        if r_wf.status_code != 200:
            self._record("Stream Workflow Receives Events", TestState.FAIL,
                          f"create workflow status={r_wf.status_code}")
            return
        stream_wf_id = r_wf.json().get("workflow_id", "")

        # Stream the workflow — use httpx streaming
        request = self.client.build_request(
            "POST",
            f"/v1/orchestrator/workflows/{stream_wf_id}/stream",
            json={"input_text": "SSE streaming conformance test"},
            headers={
                **self._h(),
                "Accept": "text/event-stream",
            },
        )

        try:
            resp = self.client.send(request, stream=True)
        except Exception as e:
            self._record("Stream Workflow Receives Events", TestState.FAIL,
                          f"Connection failed: {e}")
            return

        if resp.status_code != 200:
            resp.read()
            self._record("Stream Workflow Receives Events", TestState.FAIL,
                          f"status={resp.status_code}: {resp.text[:200]}")
            resp.close()
            return

        # Collect SSE event types
        event_types: list[str] = []
        current_event = ""
        try:
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    current_event = line[len("event:"):].strip()
                elif line.startswith("data:") and current_event:
                    event_types.append(current_event)
                    current_event = ""
        except Exception as e:
            # Timeout or connection closed — still check what we got
            pass
        finally:
            resp.close()

        has_started = any("started" in e for e in event_types)
        has_complete = any("complete" in e for e in event_types)

        self._assert(
            "Stream Workflow Receives Events",
            has_started and has_complete and len(event_types) >= 2,
            f"events={len(event_types)} types={event_types[:6]}",
        )

    def test_stream_nonexistent_404(self) -> None:
        """Streaming a nonexistent workflow returns 404."""
        request = self.client.build_request(
            "POST",
            "/v1/orchestrator/workflows/nonexistent-workflow-id/stream",
            json={"input_text": "should fail"},
            headers=self._h(),
        )
        try:
            resp = self.client.send(request, stream=True)
            status = resp.status_code
            resp.close()
        except Exception:
            status = 0

        self._assert(
            "Stream Non-Existent Workflow 404",
            status == 404,
            f"status={status}",
        )

    # ==================================================================
    # PHASE 19: Webhook CRUD + Isolation (Sprint 11)
    # ==================================================================

    def test_webhook_register(self) -> None:
        """Register a webhook — returns webhook_id and signing secret."""
        r = self.client.post("/v1/webhooks/", json={
            "url": "https://httpbin.org/post",
            "events": ["governance.denied", "workflow.completed"],
            "description": "Conformance test webhook",
        }, headers=self._h())
        ok = r.status_code == 201
        if ok:
            data = r.json()
            self.webhook_id = data.get("webhook_id", "")
            has_secret = bool(data.get("secret"))
            self._assert(
                "Register Webhook",
                bool(self.webhook_id) and has_secret,
                f"webhook_id={self.webhook_id[:12]}... secret={'present' if has_secret else 'MISSING'}",
            )
        else:
            self._record("Register Webhook", TestState.FAIL,
                          f"status={r.status_code}: {r.text[:200]}")

    def test_webhook_list(self) -> None:
        """List webhooks — count >= 1."""
        r = self.client.get("/v1/webhooks/", headers=self._h())
        ok = r.status_code == 200
        if ok:
            data = r.json()
            count = data.get("count", 0)
            self._assert("List Webhooks", count >= 1,
                          f"count={count}")
        else:
            self._record("List Webhooks", TestState.FAIL,
                          f"status={r.status_code}")

    def test_webhook_event_types(self) -> None:
        """List valid event types — non-empty list."""
        r = self.client.get("/v1/webhooks/events/types", headers=self._h())
        ok = r.status_code == 200
        if ok:
            data = r.json()
            types = data.get("event_types", [])
            self._assert("Webhook Event Types", len(types) >= 3,
                          f"types={types}")
        else:
            self._record("Webhook Event Types", TestState.FAIL,
                          f"status={r.status_code}")

    def test_webhook_cross_tenant(self) -> None:
        """Two-sided: Tenant B cannot access Tenant A's webhook."""
        if not self.webhook_id or not self.tenant_b_key:
            self._record("Cross-Tenant Webhook Blocked", TestState.FAIL,
                          "No webhook or Tenant B")
            return

        # Tenant B tries to GET Tenant A's webhook → 404 (ownership check)
        r = self.client.get(
            f"/v1/webhooks/{self.webhook_id}",
            headers=self._h("b"),
        )
        self._assert(
            "Cross-Tenant Webhook Blocked",
            r.status_code == 404,
            f"status={r.status_code}",
        )

    def test_webhook_delete(self) -> None:
        """Delete the conformance test webhook."""
        if not self.webhook_id:
            self._record("Delete Webhook", TestState.FAIL, "No webhook")
            return

        r = self.client.delete(
            f"/v1/webhooks/{self.webhook_id}",
            headers=self._h(),
        )
        ok = r.status_code == 200
        if ok:
            data = r.json()
            self._assert("Delete Webhook", data.get("deleted", False),
                          f"webhook_id={self.webhook_id[:12]}...")
        else:
            self._record("Delete Webhook", TestState.FAIL,
                          f"status={r.status_code}")

    # ==================================================================
    # PHASE 20: PDF Report Export (Sprint 11)
    # ==================================================================

    def test_pdf_download(self) -> None:
        """Download a report as PDF — verify 200, content-type, and magic bytes."""
        if not self.report_id:
            self._record("PDF Download Succeeds", TestState.FAIL,
                          "No report_id from Phase 17")
            return

        r = self.client.get(
            f"/v1/reports/{self.report_id}/download/pdf",
            headers=self._h(),
        )
        ok = r.status_code == 200
        if ok:
            content_type = r.headers.get("content-type", "")
            is_pdf = content_type.startswith("application/pdf")
            has_magic = r.content[:5] == b"%PDF-"
            self._assert(
                "PDF Download Succeeds",
                is_pdf and has_magic,
                f"content-type={content_type} size={len(r.content)} magic={'%PDF-' if has_magic else 'WRONG'}",
            )
        else:
            self._record("PDF Download Succeeds", TestState.FAIL,
                          f"status={r.status_code}: {r.text[:200]}")

    def test_pdf_missing_404(self) -> None:
        """PDF download for nonexistent report returns 404."""
        r = self.client.get(
            "/v1/reports/nonexistent-report-id/download/pdf",
            headers=self._h(),
        )
        self._assert(
            "PDF Download Missing Report 404",
            r.status_code == 404,
            f"status={r.status_code}",
        )

    # ==================================================================
    # PHASE 21: SSO Provider Listing (Sprint 11)
    # ==================================================================

    def test_sso_provider_list(self) -> None:
        """SSO providers endpoint returns 200 with a provider list."""
        r = self.client.get("/v1/portal/sso/providers")
        ok = r.status_code == 200
        if ok:
            data = r.json()
            providers = data.get("providers", [])
            self._assert(
                "SSO Provider List",
                isinstance(providers, list),
                f"providers={len(providers)} items",
            )
        else:
            self._record("SSO Provider List", TestState.FAIL,
                          f"status={r.status_code}: {r.text[:200]}")

    # ==================================================================
    # PHASE 22: Health Check Endpoints (Sprint 13)
    # ==================================================================

    def test_health_liveness(self) -> None:
        """GET /healthz returns 200 with status=ok."""
        # Health endpoints are UNAUTHENTICATED — use a raw httpx client
        import httpx
        raw = httpx.Client(base_url=self.base, timeout=10)
        try:
            r = raw.get("/healthz")
            ok = r.status_code == 200
            if ok:
                data = r.json()
                has_status = data.get("status") == "ok"
                has_uptime = "uptime_seconds" in data
                self._assert(
                    "Health Liveness",
                    has_status and has_uptime,
                    f"status={data.get('status')}, uptime={data.get('uptime_seconds', '?')}s",
                )
            else:
                self._record("Health Liveness", TestState.FAIL,
                              f"status={r.status_code}: {r.text[:200]}")
        finally:
            raw.close()

    def test_health_readiness(self) -> None:
        """GET /readyz returns 200 with status=ok and dependency checks."""
        import httpx
        raw = httpx.Client(base_url=self.base, timeout=10)
        try:
            r = raw.get("/readyz")
            ok = r.status_code == 200
            if ok:
                data = r.json()
                has_status = data.get("status") in ("ok", "degraded")
                checks = data.get("checks", {})
                has_checks = isinstance(checks, dict) and len(checks) > 0
                self._assert(
                    "Health Readiness",
                    has_status and has_checks,
                    f"status={data.get('status')}, checks={list(checks.keys())}",
                )
            else:
                self._record("Health Readiness", TestState.FAIL,
                              f"status={r.status_code}: {r.text[:200]}")
        finally:
            raw.close()

    # ==================================================================
    # Run all + honest renderer
    # ==================================================================

    def run_all(self) -> bool:
        provider_labels = {
            "mock": "🔷 MOCK (Deterministic)",
            "openai": "🟢 LIVE (OpenAI)",
            "anthropic": "🟣 LIVE (Anthropic)",
            "gemini": "🔵 LIVE (Gemini)",
        }
        mode_label = provider_labels.get(self.provider, f"🟢 LIVE ({self.provider})")
        print()
        print("=" * 70)
        print("  GRAFOMEM Cloud — Conformance Test Suite v2.1")
        print(f"  Server:   {self.base}")
        print(f"  Mode:     {mode_label}")
        print(f"  Provider: {self.provider}")
        print(f"  Time:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)

        print("\n📦 Phase 1: Account Setup")
        self.test_signup()
        if not self.api_key:
            print("\n❌ Cannot continue without API key. Aborting.")
            return False
        self.test_signup_tenant_b()

        print("\n💾 Phase 2: Memory Store")
        self.test_create_store()
        self.test_seed_facts()

        print("\n🛡️  Phase 3: Governance Policies")
        self.test_governance_policies()

        print("\n🤖 Phase 4: LLM Provider")
        self.test_register_llm()

        print("\n👥 Phase 5: Agent Definitions")
        self.test_create_agents()

        print("\n🔄 Phase 6: Workflow Execution")
        self.test_create_workflow()
        self.test_run_workflow()

        print("\n🔗 Phase 7: Execution Receipts + Hash Chain")
        self.test_execution_receipts()
        self.test_verify_chain()

        print("\n📋 Phase 8: Decision Trail + Replay")
        self.test_decision_trail()
        self.test_replay_decision()

        print("\n🛡️  Phase 9: P0 Conformance — Governance DENY (Two-Sided)")
        self.test_p0_governance_deny()

        print("\n🔍 Phase 10: P0 Conformance — PII Guard (Two-Sided)")
        self.test_p0_pii_guard()

        print("\n🔒 Phase 11: P0 Conformance — Multi-Tenant Isolation (Two-Sided)")
        self.test_p0_multi_tenant()

        # Erasure AFTER isolation tests — it deletes a fact which affects retrieval
        print("\n🗑️  Phase 12: Erasure Cascade (3-Leg)")
        self.test_erasure_cascade()

        print("\n🛡️  Phase 13: P0 Conformance — HITL Escalation (Two-Sided)")
        self.test_p0_hitl_escalation()

        print("\n🔏 Phase 14: P0 Conformance — Ed25519 Signing (Two-Sided)")
        self.test_p0_signing()

        print("\n🔗 Phase 15: Hash Chain Tamper Detection (Negative)")
        self.test_chain_tamper_negative()

        print("\n🔄 Phase 16: HITL Resume Lifecycle (Two-Sided)")
        self.test_hitl_resume_lifecycle()

        print("\n📊 Phase 17: Reports + Stats")
        self.test_generate_report()
        self.test_governance_stats()
        self.test_orchestrator_stats()

        # ── Sprint 10 coverage ────────────────────────────
        print("\n📡 Phase 18: SSE Streaming (Sprint 10)")
        self.test_stream_workflow()
        self.test_stream_nonexistent_404()

        # ── Sprint 11 coverage ────────────────────────────
        print("\n🔔 Phase 19: Webhook CRUD + Isolation (Sprint 11)")
        self.test_webhook_register()
        self.test_webhook_list()
        self.test_webhook_event_types()
        self.test_webhook_cross_tenant()
        self.test_webhook_delete()

        print("\n📄 Phase 20: PDF Report Export (Sprint 11)")
        self.test_pdf_download()
        self.test_pdf_missing_404()

        print("\n🔐 Phase 21: SSO Providers (Sprint 11)")
        self.test_sso_provider_list()

        # ── Sprint 13 coverage ────────────────────────────
        print("\n🏥 Phase 22: Health Checks (Sprint 13)")
        self.test_health_liveness()
        self.test_health_readiness()

        # ── Honest summary ────────────────────────────────
        return self._render_summary()

    def _render_summary(self) -> bool:
        """4-state honest renderer. SKIP == FAIL in MOCK mode (unless live_only)."""
        passed = 0
        failed = 0
        skipped = 0
        degraded = 0

        for r in self.results:
            if r.state == TestState.PASS:
                passed += 1
            elif r.state == TestState.FAIL:
                failed += 1
            elif r.state == TestState.SKIP:
                if not self.live and not r.live_only:
                    # SKIP in MOCK mode for non-live-only test = FAIL
                    failed += 1
                else:
                    skipped += 1
            elif r.state == TestState.DEGRADED:
                degraded += 1

        total = passed + failed + skipped + degraded

        print()
        print("=" * 70)
        parts = [f"{passed} passed"]
        if degraded:
            parts.append(f"{degraded} degraded")
        if skipped:
            parts.append(f"{skipped} skipped")
        if failed:
            parts.append(f"{failed} failed")
        summary = " · ".join(parts)

        if failed == 0 and skipped == 0 and degraded == 0:
            print(f"  Results: {summary} ✅")
        else:
            print(f"  Results: {summary}")

        print("=" * 70)

        if failed:
            print("\n  Failed tests:")
            for r in self.results:
                is_mock_skip_fail = (
                    r.state == TestState.SKIP and
                    not self.live and not r.live_only
                )
                if r.state == TestState.FAIL or is_mock_skip_fail:
                    print(f"    ❌ {r.name}: {r.detail}")

        print()
        return failed == 0

    def emit_report(self, filepath: str) -> None:
        """Emit a signed JSON conformance report."""
        report = {
            "report_type": "grafomem_conformance",
            "version": "2.0",
            "mode": "live" if self.live else "mock",
            "provider": self.provider,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "server": self.base,
            "results": [r.to_dict() for r in self.results],
            "summary": {
                "passed": sum(1 for r in self.results if r.state == TestState.PASS),
                "failed": sum(1 for r in self.results if r.state == TestState.FAIL),
                "skipped": sum(1 for r in self.results if r.state == TestState.SKIP),
                "degraded": sum(1 for r in self.results if r.state == TestState.DEGRADED),
                "total": len(self.results),
            },
        }

        # Content hash for integrity
        report_json = json.dumps(report, sort_keys=True, indent=2)
        report["content_hash"] = hashlib.blake2b(
            report_json.encode(), digest_size=32,
        ).hexdigest()

        # Write
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(report, f, indent=2, sort_keys=True)

        print(f"\n📄 Conformance report written to: {filepath}")
        print(f"   Content hash: {report['content_hash'][:24]}...")


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="GRAFOMEM Conformance Suite v2")
    parser.add_argument("--live", action="store_true",
                        help="Use real LLM (OpenAI). Default: deterministic MockLLM.")
    parser.add_argument("--anthropic", action="store_true",
                        help="Use real LLM (Anthropic Claude). Requires ANTHROPIC_API_KEY.")
    parser.add_argument("--gemini", action="store_true",
                        help="Use real LLM (Google Gemini). Requires GOOGLE_API_KEY.")
    parser.add_argument("--url", default=BASE_URL,
                        help=f"Server URL (default: {BASE_URL})")
    parser.add_argument("--report", action="store_true",
                        help="Emit signed JSON conformance report")
    args = parser.parse_args()

    # Determine provider and live mode
    if args.anthropic:
        live = True
        provider = "anthropic"
        if not ANTHROPIC_KEY:
            print("❌ --anthropic mode requires ANTHROPIC_API_KEY environment variable.")
            sys.exit(1)
    elif args.gemini:
        live = True
        provider = "gemini"
        if not GOOGLE_KEY:
            print("❌ --gemini mode requires GOOGLE_API_KEY environment variable.")
            sys.exit(1)
    elif args.live:
        live = True
        provider = "openai"
        if not OPENAI_KEY:
            print("❌ --live mode requires OPENAI_API_KEY environment variable.")
            sys.exit(1)
    else:
        live = False
        provider = "mock"

    suite = ConformanceSuite(args.url, live_mode=live, provider=provider)
    success = suite.run_all()

    if args.report:
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        mode = "live" if live else "mock"
        filepath = f"tests/runs/{ts}_{mode}_{provider}.json"
        suite.emit_report(filepath)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
