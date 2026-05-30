#!/usr/bin/env python3
"""Integration test for the GRAFOMEM SDK.

Runs against a local sandbox server at http://localhost:8080.
Tests every service module end-to-end.

Usage:
    PYTHONPATH=sdk/src python3 tests/test_sdk_integration.py
"""

import sys
import os

# Ensure the SDK source is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sdk", "src"))

from grafomem import GrafomemClient, NotFoundError, AuthenticationError
from grafomem.types import GovernanceStats

BASE_URL = "http://localhost:8080"
PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    """Assert a test condition."""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ✅ {name}{f' — {detail}' if detail else ''}")
    else:
        FAILED += 1
        print(f"  ❌ {name}{f' — {detail}' if detail else ''}")


def main() -> None:
    global PASSED, FAILED

    print("=" * 60)
    print("  GRAFOMEM SDK — Integration Test")
    print(f"  Server: {BASE_URL}")
    print("=" * 60)
    print()

    # ── Phase 1: Portal (signup) ──────────────────────────────────
    print("📦 Phase 1: Portal")
    client = GrafomemClient(base_url=BASE_URL)

    tenant = client.portal.signup(
        name="SDK Test",
        email="sdk-test@example.com",
        password="testpassword123",
    )
    check("Signup", bool(tenant.api_key), f"api_key={tenant.api_key[:12]}...")

    # Reconnect with the API key
    client.close()
    client = GrafomemClient(api_key=tenant.api_key, base_url=BASE_URL)
    print()

    # ── Phase 2: Stores ───────────────────────────────────────────
    print("💾 Phase 2: Stores")
    store = client.stores.create(name="sdk-test-store")
    check("Create Store", bool(store.id), f"id={store.id}")

    stores = client.stores.list()
    check("List Stores", len(stores) >= 1, f"count={len(stores)}")
    print()

    # ── Phase 3: Memories ─────────────────────────────────────────
    print("🧠 Phase 3: Memories")
    w1 = client.memories.write(store.id, content="User prefers dark mode", source="test")
    check("Write Memory", w1.ref is not None and w1.ref != 0, f"ref={w1.ref}")

    w2 = client.memories.write(store.id, content="User lives in Barcelona", source="test")
    check("Write Memory 2", w2.ref is not None and w2.ref != 0, f"ref={w2.ref}")

    w3 = client.memories.write(store.id, content="Favorite language is Python", source="test")
    check("Write Memory 3", w3.ref is not None and w3.ref != 0, f"ref={w3.ref}")

    results = client.memories.retrieve(store.id, query="user preferences", top_k=3)
    check("Retrieve Memories", len(results) >= 1, f"count={len(results)}")
    check("Retrieve Has Content", bool(results[0].content), f"content={results[0].content[:30]}...")

    client.memories.delete(store.id, ref=w3.ref)
    results_after = client.memories.retrieve(store.id, query="Python", top_k=5)
    deleted_found = any(r.ref == w3.ref for r in results_after)
    check("Delete Memory", not deleted_found, f"ref={w3.ref} removed")
    print()

    # ── Phase 4: Governance ───────────────────────────────────────
    print("🛡️  Phase 4: Governance")
    policy = client.governance.create_policy(
        name="SDK Test PII Guard",
        policy_type="pii_guard",
        action="deny",
        config={"patterns": [r"\b\d{3}-\d{2}-\d{4}\b"]},
    )
    check("Create Policy", bool(policy.id), f"id={policy.id}")

    policies = client.governance.list_policies()
    check("List Policies", len(policies) >= 1, f"count={len(policies)}")

    # Evaluate — PII should be denied
    result = client.governance.evaluate(
        operation="output_check",
        context={"output": "SSN is 123-45-6789"},
    )
    check("Evaluate PII (DENY)", not result.allowed, f"allowed={result.allowed}")

    # Evaluate — clean input should pass
    result_clean = client.governance.evaluate(
        operation="output_check",
        context={"output": "Hello world"},
    )
    check("Evaluate Clean (ALLOW)", result_clean.allowed, f"allowed={result_clean.allowed}")

    stats = client.governance.stats()
    has_data = isinstance(stats, dict) or (hasattr(stats, 'total_evaluations') and stats.total_evaluations > 0)
    check("Governance Stats", isinstance(stats, (dict, GovernanceStats)), f"type={type(stats).__name__}")
    print()

    # ── Phase 5: LLM & Tools ─────────────────────────────────────
    print("🤖 Phase 5: LLM & Tools")
    provider = client.llm.register_provider(
        model_id="mock-model", provider="mock", api_key="mock-key",
    )
    check("Register Provider", bool(provider.model_id), f"model_id={provider.model_id}")

    providers = client.llm.list_providers()
    check("List Providers", len(providers) >= 1, f"count={len(providers)}")
    print()

    # ── Phase 6: Orchestrator ─────────────────────────────────────
    print("🔄 Phase 6: Orchestrator")
    agent = client.orchestrator.create_agent(
        name="test-agent",
        role="researcher",
        model_id="mock-model",
        system_prompt="You are a test agent.",
        store_id=store.id,
    )
    check("Create Agent", bool(agent.id), f"id={agent.id}")

    agents = client.orchestrator.list_agents()
    check("List Agents", len(agents) >= 1, f"count={len(agents)}")

    workflow = client.orchestrator.create_workflow(
        name="sdk-test-workflow",
        agents=[agent.id],
        input_text="Test the SDK",
    )
    check("Create Workflow", bool(workflow.id), f"id={workflow.id}")

    run = client.orchestrator.run_workflow(workflow.id, input="Test the SDK")
    check("Run Workflow", run.status == "completed", f"status={run.status}")
    check("Workflow Has Steps", len(run.steps) >= 1, f"steps={len(run.steps)}")

    # Receipts
    receipts = client.orchestrator.receipts(workflow.id)
    check("Get Receipts", len(receipts) >= 1, f"count={len(receipts)}")

    # Chain verification
    chain = client.orchestrator.verify_chain(workflow.id)
    check("Verify Chain", chain.status == "intact", f"status={chain.status}")

    # Stats (may be dict or typed)
    try:
        orch_stats = client.orchestrator.stats()
        check("Orchestrator Stats", orch_stats is not None, f"type={type(orch_stats).__name__}")
    except Exception as e:
        check("Orchestrator Stats", False, f"error={e}")
    print()

    # ── Phase 7: Decisions ────────────────────────────────────────
    print("📋 Phase 7: Decisions")
    decisions = client.decisions.list(limit=5)
    check("List Decisions", len(decisions) >= 1, f"count={len(decisions)}")

    if decisions:
        decision = client.decisions.get(decisions[0].id)
        check("Get Decision", bool(decision.id), f"id={decision.id}")

        # Replay
        replay = client.orchestrator.replay(decision.id)
        check("Replay Decision", replay.status in ("identical", "diverged"), f"status={replay.status}")
    print()

    # ── Phase 8: Erasure ──────────────────────────────────────────
    print("🗑️  Phase 8: Erasure")
    cert = client.erasure.issue(
        fact_ref=int(w1.ref),
        reason="GDPR Art 17 — SDK test",
    )
    check("Issue Certificate", bool(cert.certificate_id), f"id={cert.certificate_id}")

    verification = client.erasure.verify(cert.certificate_id)
    check("Verify Certificate", verification.valid, f"detail={verification.detail[:40]}...")

    certs = client.erasure.list()
    check("List Certificates", len(certs) >= 1, f"count={len(certs)}")
    print()

    # ── Phase 9: Reports ──────────────────────────────────────────
    print("📊 Phase 9: Reports")
    report = client.reports.generate(framework="eu_ai_act")
    check("Generate Report", bool(report.id), f"id={report.id}")
    check("Report Has Framework", bool(report.framework), f"framework={report.framework}")
    print()

    # ── Phase 10: Error Handling ──────────────────────────────────
    print("⚠️  Phase 10: Error Handling")
    try:
        bad_client = GrafomemClient(api_key="invalid-key", base_url=BASE_URL)
        bad_client.stores.list()
        check("Auth Error", False, "should have raised")
    except (AuthenticationError, Exception) as e:
        check("Auth Error", True, f"caught {type(e).__name__}")

    # ── Summary ───────────────────────────────────────────────────
    print()
    print("=" * 60)
    total = PASSED + FAILED
    if FAILED == 0:
        print(f"  Results: {PASSED} passed ✅")
    else:
        print(f"  Results: {PASSED} passed · {FAILED} failed")
    print("=" * 60)

    client.close()
    sys.exit(1 if FAILED > 0 else 0)


if __name__ == "__main__":
    main()
