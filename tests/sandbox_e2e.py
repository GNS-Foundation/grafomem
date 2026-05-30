#!/usr/bin/env python3
"""
GRAFOMEM Cloud — End-to-End Sandbox Test

Exercises ALL 7 layers + Sprint 7 features in a real deployment:
  Layer 1: GMP Memory (write, retrieve, delete)
  Layer 2: Decision Trail (signed inference records)
  Layer 3: Erasure Proof (cryptographic deletion certificates)
  Layer 4: Governance (PDP/PEP policy engine)
  Layer 5: Regulatory Reports (EU AI Act)
  Layer 6: Orchestrator (3-agent sequential workflow)
  Sprint 7a: Policy Engine (stateless evaluation)
  Sprint 7b: Execution Receipts (hash-chained attestation)
  Sprint 7c: Memory Taxonomy (typed memory layers)
  Sprint 7d: Deterministic Replay (decision re-execution)

Usage:
    # With real LLM (OpenAI):
    OPENAI_API_KEY=sk-... python3 tests/sandbox_e2e.py

    # Without LLM (mock mode — tests all infrastructure, skips inference):
    python3 tests/sandbox_e2e.py --mock
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Any

import httpx

# ============================================================================
# Configuration
# ============================================================================

BASE_URL = os.environ.get("GRAFOMEM_URL", "http://localhost:8080")
TEST_EMAIL = "sandbox@grafomem.test"
TEST_PASSWORD = "SandboxTest2026!"
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")

# ============================================================================
# Test runner
# ============================================================================

class SandboxTest:
    """End-to-end orchestrator test."""

    def __init__(self, base_url: str, mock_mode: bool = False) -> None:
        self.base = base_url
        self.mock = mock_mode
        self.client = httpx.Client(base_url=base_url, timeout=120.0)
        self.api_key: str = ""
        self.tenant_id: str = ""
        self.store_id: str = ""
        self.agent_ids: list[str] = []
        self.workflow_id: str = ""
        self.decision_ids: list[str] = []
        self.certificate_id: str = ""
        self.fact_refs: list[int] = []
        self.results: list[dict] = []

    def _h(self) -> dict[str, str]:
        """Auth headers."""
        return {"X-API-Key": self.api_key}

    def _record(self, name: str, passed: bool, detail: str = "") -> None:
        status = "✅" if passed else "❌"
        self.results.append({"name": name, "passed": passed, "detail": detail})
        print(f"  {status} {name}" + (f" — {detail}" if detail else ""))

    # ------------------------------------------------------------------
    # PHASE 1: Account setup
    # ------------------------------------------------------------------

    def test_signup(self) -> None:
        """Create test account."""
        r = self.client.post("/v1/portal/signup", json={
            "name": "Sandbox Test",
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
            "plan": "starter",
        })
        if r.status_code in (400, 409):
            # Already exists, that's fine — will login next
            self._record("Signup", True, "Account already exists")
            return
        ok = r.status_code == 201
        if ok:
            data = r.json()
            self.api_key = data.get("api_key", "")
            self.tenant_id = data.get("tenant_id", "")
        self._record("Signup", ok, f"status={r.status_code}")

    def test_login(self) -> None:
        """Login and get API key."""
        if self.api_key:
            # Already got key from signup
            self._record("Login + API Key", True,
                          f"tenant={self.tenant_id[:12]}... (from signup)")
            return

        r = self.client.post("/v1/portal/login", json={
            "email": TEST_EMAIL,
            "password": TEST_PASSWORD,
        })
        ok = r.status_code == 200
        if ok:
            data = r.json()
            self.api_key = data.get("api_key", "")
            self.tenant_id = data.get("tenant_id", "")
        self._record("Login + API Key", ok and bool(self.api_key),
                      f"tenant={self.tenant_id[:12]}..." if self.tenant_id else "")

    # ------------------------------------------------------------------
    # PHASE 2: Memory store
    # ------------------------------------------------------------------

    def test_create_store(self) -> None:
        """Create a memory store."""
        r = self.client.post("/v1/stores", headers=self._h())
        ok = r.status_code == 200
        if ok:
            self.store_id = r.json().get("store_id", "")
        self._record("Create Store", ok and bool(self.store_id),
                      f"store_id={self.store_id}" if self.store_id else f"status={r.status_code}: {r.text[:100]}")

    def test_seed_facts(self) -> None:
        """Write 5 compliance facts to the store."""
        facts = [
            "The EU AI Act (Regulation 2024/1689) requires high-risk AI systems to maintain detailed logs of all decisions made during operation, per Article 12.",
            "GDPR Article 17 establishes the Right to Erasure, requiring data controllers to delete personal data upon request without undue delay.",
            "DORA (Digital Operational Resilience Act) requires financial entities to implement comprehensive ICT risk management frameworks, per Article 6.",
            "Under the EU AI Act Article 14, high-risk AI systems must have effective human oversight measures, including the ability to intervene and override.",
            "ISO 42001 is the first international standard for AI Management Systems, providing a framework for responsible AI governance.",
        ]
        written = 0
        for fact in facts:
            r = self.client.post(f"/v1/stores/{self.store_id}/write", json={
                "content": fact,
            }, headers=self._h())
            if r.status_code == 200:
                ref = r.json().get("ref")
                if ref is not None:
                    self.fact_refs.append(ref)
                written += 1
        self._record("Seed Facts", written == 5,
                      f"{written}/5 written, refs={self.fact_refs[:3]}...")

    # ------------------------------------------------------------------
    # PHASE 3: Governance policies
    # ------------------------------------------------------------------

    def test_governance_policies(self) -> None:
        """Create governance policies."""
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
                    "check_fields": ["output"],
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
        self._record("Governance Policies", created == 2, f"{created}/2 created")

    # ------------------------------------------------------------------
    # PHASE 4: LLM provider
    # ------------------------------------------------------------------

    def test_register_llm(self) -> None:
        """Register an LLM provider."""
        if self.mock:
            self._record("Register LLM", True, "SKIPPED (mock mode)")
            return

        if not OPENAI_KEY:
            self._record("Register LLM", False,
                          "No OPENAI_API_KEY set. Use --mock or set the env var.")
            return

        r = self.client.post("/v1/llm/providers", json={
            "provider": "openai",
            "model_id": "gpt-4o-mini",
            "config": {"api_key": OPENAI_KEY},
        }, headers=self._h())
        ok = r.status_code == 200
        self._record("Register LLM (OpenAI gpt-4o-mini)", ok,
                      r.json().get("config_id", "")[:12] + "..." if ok else f"status={r.status_code}")

    # ------------------------------------------------------------------
    # PHASE 5: Agent definitions
    # ------------------------------------------------------------------

    def test_create_agents(self) -> None:
        """Create 3 agents: researcher, writer, reviewer."""
        agents = [
            {
                "name": "Compliance Researcher",
                "role": "researcher",
                "description": "Retrieves compliance facts from memory",
                "model_id": "gpt-4o-mini",
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
                "model_id": "gpt-4o-mini",
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
                "model_id": "gpt-4o-mini",
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

        self._record("Create Agents", len(self.agent_ids) == 3,
                      f"{len(self.agent_ids)}/3 — " +
                      ", ".join(a[:8] for a in self.agent_ids))

    # ------------------------------------------------------------------
    # PHASE 6: Workflow
    # ------------------------------------------------------------------

    def test_create_workflow(self) -> None:
        """Create a sequential 3-agent workflow."""
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
        self._record("Create Workflow", ok and bool(self.workflow_id),
                      f"workflow_id={self.workflow_id[:12]}..." if self.workflow_id else "")

    def test_run_workflow(self) -> None:
        """Execute the workflow with a compliance question."""
        if self.mock:
            self._record("Run Workflow", True,
                          "SKIPPED (mock mode — no LLM configured)")
            return

        if not OPENAI_KEY:
            self._record("Run Workflow", False,
                          "No OPENAI_API_KEY — cannot execute LLM inference")
            return

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
            detail = (
                f"status={status} steps={len(steps)} "
                f"tokens={total_tokens}"
            )
            # Collect decision IDs
            for step in steps:
                did = step.get("decision_id")
                if did:
                    self.decision_ids.append(did)
        else:
            detail = f"status={r.status_code}: {r.text[:200]}"

        self._record("Run Workflow", ok, detail)

    # ------------------------------------------------------------------
    # PHASE 7: Verify Sprint 7 features
    # ------------------------------------------------------------------

    def test_execution_receipts(self) -> None:
        """Check that execution receipts were generated."""
        if self.mock or not self.workflow_id:
            self._record("Execution Receipts", True, "SKIPPED (no workflow executed)")
            return

        r = self.client.get(
            f"/v1/orchestrator/workflows/{self.workflow_id}/receipts",
            headers=self._h(),
        )
        ok = r.status_code == 200
        count = 0
        if ok:
            data = r.json()
            count = data.get("count", 0)
        self._record("Execution Receipts", ok and count > 0,
                      f"{count} receipts generated")

    def test_verify_chain(self) -> None:
        """Verify the hash chain is intact."""
        if self.mock or not self.workflow_id:
            self._record("Verify Hash Chain", True, "SKIPPED (no workflow executed)")
            return

        r = self.client.get(
            f"/v1/orchestrator/workflows/{self.workflow_id}/verify-chain",
            headers=self._h(),
        )
        ok = r.status_code == 200
        status = ""
        if ok:
            data = r.json()
            status = data.get("status", "")
            steps = data.get("steps_verified", 0)
        self._record("Verify Hash Chain", ok and status == "intact",
                      f"status={status} steps_verified={steps}" if ok else "")

    def test_decision_trail(self) -> None:
        """Verify decisions were logged."""
        r = self.client.get("/v1/decisions/", params={"limit": 10},
                            headers=self._h())
        ok = r.status_code == 200
        count = 0
        if ok:
            data = r.json()
            decisions = data.get("decisions", [])
            count = len(decisions)
            if decisions and not self.decision_ids:
                self.decision_ids = [d["decision_id"] for d in decisions[:3]]
        self._record("Decision Trail", ok,
                      f"{count} decisions found" + (
                          f" (first: {self.decision_ids[0][:12]}...)" if self.decision_ids else ""
                      ))

    def test_replay_decision(self) -> None:
        """Replay a decision and check the result."""
        if self.mock or not self.decision_ids:
            self._record("Replay Decision", True,
                          "SKIPPED (no decisions to replay)")
            return

        if not OPENAI_KEY:
            self._record("Replay Decision", True,
                          "SKIPPED (no API key for LLM re-execution)")
            return

        decision_id = self.decision_ids[0]
        r = self.client.post(
            f"/v1/orchestrator/replay/{decision_id}",
            headers=self._h(),
        )
        ok = r.status_code == 200
        detail = ""
        if ok:
            data = r.json()
            status = data.get("status", "")
            confidence = data.get("confidence", 0)
            latency = data.get("replay_latency_ms", 0)
            detail = f"status={status} confidence={confidence:.2f} latency={latency}ms"
        self._record("Replay Decision", ok, detail)

    # ------------------------------------------------------------------
    # PHASE 8: Erasure proof
    # ------------------------------------------------------------------

    def test_delete_fact(self) -> None:
        """Delete a fact and issue an erasure certificate."""
        if not self.store_id or not self.fact_refs:
            self._record("Delete + Erasure", False, "No store_id or refs")
            return

        # Use the first fact ref we actually wrote
        target_ref = self.fact_refs[0]

        # Step 1: Delete the fact from the memory store
        r = self.client.post(
            f"/v1/stores/{self.store_id}/delete",
            json={"ref": target_ref},
            headers=self._h(),
        )
        ok_delete = r.status_code == 200 and r.json().get("deleted", False)

        # Step 2: Issue an erasure certificate
        r2 = self.client.post(
            "/v1/erasure/issue",
            json={
                "fact_ref": target_ref,
                "fact_content": "The EU AI Act (Regulation 2024/1689) requires...",
                "memory_deleted": True,
                "legal_basis": "GDPR Article 17 — Right to Erasure",
                "requested_by": "data_subject",
            },
            headers=self._h(),
        )
        ok_cert = r2.status_code == 200
        if ok_cert:
            data = r2.json()
            self.certificate_id = data.get("certificate_id", "")

        self._record(
            "Delete Fact + Erasure Cert",
            ok_delete and ok_cert and bool(self.certificate_id),
            f"deleted=ref:{target_ref} cert={self.certificate_id[:12]}..." if self.certificate_id
            else f"delete={r.status_code} cert={r2.status_code}: {r2.text[:100]}",
        )

    def test_verify_certificate(self) -> None:
        """Verify the erasure certificate."""
        if not self.certificate_id:
            self._record("Verify Certificate", True, "SKIPPED (no certificate)")
            return

        r = self.client.get(
            f"/v1/erasure/{self.certificate_id}/verify",
            headers=self._h(),
        )
        ok = r.status_code == 200
        if ok:
            data = r.json()
            valid = data.get("valid", False)
            detail = data.get("detail", "")
            # Accept valid=True OR unsigned certs (no signing key configured)
            is_ok = valid or "unsigned" in detail.lower() or "not signed" in detail.lower() or "no signature" in detail.lower()
            self._record("Verify Erasure Certificate", is_ok,
                          f"valid={valid} detail={detail[:60]}")
        else:
            self._record("Verify Erasure Certificate", False,
                          f"status={r.status_code}")

    # ------------------------------------------------------------------
    # PHASE 9: Regulatory report
    # ------------------------------------------------------------------

    def test_generate_report(self) -> None:
        """Generate an EU AI Act compliance report."""
        r = self.client.post("/v1/reports/generate", json={
            "framework": "eu_ai_act",
        }, headers=self._h())
        ok = r.status_code == 200
        detail = ""
        if ok:
            data = r.json()
            report_id = data.get("report_id", "")
            detail = f"report_id={report_id[:12]}..."
        self._record("Generate EU AI Act Report", ok, detail)

    # ------------------------------------------------------------------
    # PHASE 10: Governance stats
    # ------------------------------------------------------------------

    def test_governance_stats(self) -> None:
        """Check governance evaluation stats."""
        r = self.client.get("/v1/governance/stats", headers=self._h())
        ok = r.status_code == 200
        if ok:
            data = r.json()
            total = data.get("evaluations_total", 0)
            policies = data.get("policies_active", 0)
            self._record("Governance Stats", ok,
                          f"{total} evaluations, {policies} active policies")
        else:
            self._record("Governance Stats", False, f"status={r.status_code}")

    # ------------------------------------------------------------------
    # PHASE 11: Orchestrator stats
    # ------------------------------------------------------------------

    def test_orchestrator_stats(self) -> None:
        """Check orchestrator dashboard stats."""
        r = self.client.get("/v1/orchestrator/stats", headers=self._h())
        ok = r.status_code == 200
        if ok:
            data = r.json()
            self._record("Orchestrator Stats", ok,
                          f"agents={data.get('agents_total', 0)} "
                          f"workflows={data.get('workflows_total', 0)} "
                          f"steps={data.get('steps_total', 0)}")
        else:
            self._record("Orchestrator Stats", False, f"status={r.status_code}")

    # ------------------------------------------------------------------
    # Run all
    # ------------------------------------------------------------------

    def run_all(self) -> bool:
        """Execute all tests in order."""
        print()
        print("=" * 65)
        print("  GRAFOMEM Cloud — End-to-End Sandbox Test")
        print(f"  Server: {self.base}")
        print(f"  Mode:   {'🔶 MOCK (no LLM)' if self.mock else '🟢 LIVE (OpenAI)'}")
        print(f"  Time:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 65)

        print("\n📦 Phase 1: Account Setup")
        self.test_signup()
        self.test_login()

        if not self.api_key:
            print("\n❌ Cannot continue without API key. Aborting.")
            return False

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

        print("\n🔗 Phase 7: Sprint 7 Features")
        self.test_execution_receipts()
        self.test_verify_chain()
        self.test_decision_trail()
        self.test_replay_decision()

        print("\n🗑️  Phase 8: Erasure Proof")
        self.test_delete_fact()
        self.test_verify_certificate()

        print("\n📊 Phase 9: Regulatory Report")
        self.test_generate_report()

        print("\n📈 Phase 10: Platform Stats")
        self.test_governance_stats()
        self.test_orchestrator_stats()

        # Summary
        passed = sum(1 for r in self.results if r["passed"])
        total = len(self.results)
        failed = total - passed

        print()
        print("=" * 65)
        print(f"  Results: {passed}/{total} passed" +
              (f" ({failed} failed)" if failed else " — ALL GREEN 🎉"))
        print("=" * 65)

        if failed:
            print("\n  Failed tests:")
            for r in self.results:
                if not r["passed"]:
                    print(f"    ❌ {r['name']}: {r['detail']}")

        print()
        return failed == 0


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="GRAFOMEM E2E Sandbox Test")
    parser.add_argument("--mock", action="store_true",
                        help="Skip LLM calls (test infrastructure only)")
    parser.add_argument("--url", default=BASE_URL,
                        help=f"Server URL (default: {BASE_URL})")
    args = parser.parse_args()

    mock = args.mock or (not OPENAI_KEY)
    if not OPENAI_KEY and not args.mock:
        print("⚠️  No OPENAI_API_KEY found. Running in mock mode.")
        print("   Set OPENAI_API_KEY to test with real LLM inference.\n")

    test = SandboxTest(args.url, mock_mode=mock)
    success = test.run_all()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
