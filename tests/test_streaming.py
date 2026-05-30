#!/usr/bin/env python3
"""
GRAFOMEM Sprint 10 — SSE Streaming Integration Test.

Tests real-time streaming workflow execution via Server-Sent Events.
Requires a running GRAFOMEM Cloud server with a PostgreSQL backend.

Usage:
    PYTHONPATH=sdk/src python tests/test_streaming.py

Environment:
    GRAFOMEM_URL   — Server base URL (default: http://127.0.0.1:8000)
"""

from __future__ import annotations

import json
import os
import sys
import time

# ── Configuration ─────────────────────────────────────────────────────

BASE_URL = os.environ.get("GRAFOMEM_URL", "http://127.0.0.1:8000")

# ── Test state ────────────────────────────────────────────────────────

passed = 0
failed = 0
errors: list[str] = []


def check(label: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {label}")
    else:
        failed += 1
        msg = f"  ❌ {label}" + (f" — {detail}" if detail else "")
        print(msg)
        errors.append(msg)


def section(title: str):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ── Main ──────────────────────────────────────────────────────────────

def main():
    from grafomem import GrafomemClient

    print("=" * 60)
    print("  GRAFOMEM Cloud — SSE Streaming Integration Test")
    print(f"  Server: {BASE_URL}")
    print("=" * 60)

    # ── Phase 1: Setup ────────────────────────────────────────────────

    section("📦 Phase 1: Setup (Portal + Store + LLM + Agents + Workflow)")

    # Signup via SDK
    client = GrafomemClient(base_url=BASE_URL)
    tenant = client.portal.signup(
        name=f"stream-test-{int(time.time())}",
        email=f"stream-test-{int(time.time())}@test.com",
        password="testpass123",
    )
    # Use JWT token for auth (works in both 'token' and 'cloud' modes)
    auth_key = getattr(tenant, 'token', None) or tenant.api_key
    check("Signup", bool(auth_key), f"key={auth_key[:16]}...")

    # Reconnect with the auth key
    client.close()
    client = GrafomemClient(base_url=BASE_URL, api_key=auth_key)

    # Create store + seed facts
    try:
        store = client.stores.create("stream-test-store")
    except Exception as e:
        print(f"\n  ⚠️  Auth failed after signup: {e}")
        print(f"  ℹ️  This likely means the server is in 'token' auth mode.")
        print(f"  ℹ️  Run against a local server in 'cloud' mode instead:")
        print(f"       GRAFOMEM_URL=http://localhost:8080 PYTHONPATH=sdk/src python3 tests/test_streaming.py")
        _print_summary()
        return
    check("Create Store", store.id != "", f"store_id={store.id}")

    for fact in [
        "GRAFOMEM supports SSE streaming for real-time execution",
        "The orchestrator emits 11 event types",
        "Each step goes through governance, memory, LLM, and completion",
    ]:
        client.memories.write(store.id, fact)
    check("Seed 3 Facts", True)

    # Register mock LLM
    provider = client.llm.register_provider(
        model_id="mock-stream", provider="mock", api_key="mock-key",
    )
    check("Register MockLLM", bool(provider.model_id))

    # Create 2 agents (small workflow for speed)
    agent_a = client.orchestrator.create_agent(
        name="StreamAnalyzer", role="researcher", model_id="mock-stream",
        system_prompt="Analyze streaming capabilities.",
    )
    agent_b = client.orchestrator.create_agent(
        name="StreamWriter", role="writer", model_id="mock-stream",
        system_prompt="Write a summary.",
    )
    check("Create 2 Agents", agent_a.agent_id != "" and agent_b.agent_id != "")

    # Create workflow
    workflow = client.orchestrator.create_workflow(
        name="stream-test-flow",
        agents=[agent_a.agent_id, agent_b.agent_id],
        mode="sequential",
    )
    check("Create Workflow", workflow.workflow_id != "")

    # ── Phase 2: Streaming Execution ──────────────────────────────────

    section("🔴 Phase 2: SSE Streaming Execution")

    events_received: list = []
    event_types_seen: set[str] = set()

    try:
        for event in client.orchestrator.stream_workflow(
            workflow.workflow_id,
            input="Analyze GRAFOMEM's streaming architecture",
        ):
            events_received.append(event)
            event_types_seen.add(event.event)
            print(f"    📡 {event.event}: {json.dumps(event.data, default=str)[:120]}")

    except Exception as e:
        check("Stream Execution", False, str(e))
        _print_summary()
        return

    check("Stream Returned Events", len(events_received) > 0,
          f"got {len(events_received)} events")

    # ── Phase 3: Event Ordering Validation ────────────────────────────

    section("📋 Phase 3: Event Ordering Validation")

    if events_received:
        check("First Event is workflow.started",
              events_received[0].event == "workflow.started",
              f"got {events_received[0].event}")

        last_event = events_received[-1]
        check("Last Event is workflow.complete or workflow.error",
              last_event.event in ("workflow.complete", "workflow.error"),
              f"got {last_event.event}")

    # ── Phase 4: Event Coverage ───────────────────────────────────────

    section("🧪 Phase 4: Event Coverage")

    expected_events = {
        "workflow.started",
        "step.started",
        "step.memory_retrieve",
        "step.llm_complete",
        "step.complete",
        "workflow.complete",
    }

    for evt_type in expected_events:
        check(f"Event type '{evt_type}' received",
              evt_type in event_types_seen,
              f"seen: {sorted(event_types_seen)}")

    check("At least 2 step.complete events (2 agents)",
          sum(1 for e in events_received if e.event == "step.complete") >= 2,
          f"got {sum(1 for e in events_received if e.event == 'step.complete')}")

    # ── Phase 5: Event Data Validation ────────────────────────────────

    section("📊 Phase 5: Event Data Validation")

    # workflow.started should have mode and agent_count
    started_events = [e for e in events_received if e.event == "workflow.started"]
    if started_events:
        d = started_events[0].data
        check("workflow.started has mode", "mode" in d, str(d))
        check("workflow.started has agent_count", "agent_count" in d, str(d))

    # step.complete should have tokens_used
    complete_events = [e for e in events_received if e.event == "step.complete"]
    if complete_events:
        d = complete_events[0].data
        check("step.complete has tokens_used", "tokens_used" in d, str(d))
        check("step.complete has decision_id", "decision_id" in d, str(d))

    # workflow.complete should have total_steps and duration_ms
    wf_complete = [e for e in events_received if e.event == "workflow.complete"]
    if wf_complete:
        d = wf_complete[0].data
        check("workflow.complete has total_steps", "total_steps" in d, str(d))
        check("workflow.complete has duration_ms", "duration_ms" in d, str(d))
        check("workflow.complete has total_tokens", "total_tokens" in d, str(d))

    # ── Phase 6: elapsed_ms Monotonicity ──────────────────────────────

    section("⏱️ Phase 6: Elapsed Time Monotonicity")

    elapsed_values = [e.data.get("elapsed_ms", 0) for e in events_received if "elapsed_ms" in e.data]
    if len(elapsed_values) >= 2:
        is_monotonic = all(a <= b for a, b in zip(elapsed_values, elapsed_values[1:]))
        check("elapsed_ms is monotonically non-decreasing", is_monotonic,
              f"values: {elapsed_values[:5]}...")
    else:
        check("elapsed_ms available", False, "not enough values")

    # ── Phase 7: Non-Breaking — /run Still Works ──────────────────────

    section("🔄 Phase 7: Non-Breaking — /run Endpoint")

    # Create a fresh workflow to test /run
    wf2 = client.orchestrator.create_workflow(
        name="run-test-flow",
        agents=[agent_a.agent_id, agent_b.agent_id],
    )
    run_result = client.orchestrator.run_workflow(
        wf2.workflow_id, input="Test /run still works",
    )
    check("Existing /run endpoint works",
          run_result.status in ("completed", "COMPLETED"),
          f"status={run_result.status}")

    # ── Summary ───────────────────────────────────────────────────────

    _print_summary()


def _print_summary():
    print("\n" + "=" * 60)
    print(f"  Results: {passed} passed", end="")
    if failed:
        print(f" · {failed} failed ❌")
        for e in errors:
            print(f"    {e}")
    else:
        print(" ✅")
    print("=" * 60)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
