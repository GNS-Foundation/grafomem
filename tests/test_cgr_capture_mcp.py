"""Unit tests for the grafomem-cgr capture MCP server (Track C, ticket 1).

Pure-logic coverage — no network, no DB. The integration/full-loop acceptance
(decision → outcome → score movement) runs against the dogfood tenant via
`python ops/cgr_capture_mcp.py selftest` and is captured in the PR.
"""
from __future__ import annotations

import json

import pytest

from ops.cgr_capture_mcp import (
    CORP_TENANT,
    DEV_DOMAINS,
    CaptureClient,
    Config,
    _assert_not_corp,
    map_dev_outcome,
)


# ── outcome mapping (invariant 4) ────────────────────────────────────────────

@pytest.mark.parametrize("result,expected", [
    ("deploy_succeeded", "paid"), ("scan_clean", "paid"), ("review_confirmed", "paid"),
    ("deploy_rolled_back", "default"), ("vuln_found", "default"), ("review_refuted", "default"),
])
def test_outcome_mapping_scored(result, expected):
    assert map_dev_outcome(result) == expected


def test_outcome_mapping_unmapped_is_none():
    # ambiguous / in-flight ⇒ None ⇒ no-op ⇒ decision left pending (never falsely resolved)
    assert map_dev_outcome("still_running") is None
    assert map_dev_outcome("") is None


# ── never-corp guard (invariant 1) ───────────────────────────────────────────

def test_assert_not_corp_refuses_corp():
    with pytest.raises(SystemExit):
        _assert_not_corp(CORP_TENANT, where="test")


def test_assert_not_corp_allows_other():
    _assert_not_corp("some-dogfood-tenant", where="test")  # no raise


def _cfg(tenant="dogfood-t", roles=None):
    c = Config.__new__(Config)
    c.base_url = "https://api.grafomem.com"
    c.tenant_key = "k"
    c.dogfood_tenant = tenant
    c.role_keys = roles if roles is not None else {"cc-builder@ulissy": "aa" * 32}
    return c


def test_config_validate_refuses_corp_tenant():
    with pytest.raises(SystemExit):
        _cfg(tenant=CORP_TENANT).validate()


def test_config_validate_requires_role_keys():
    with pytest.raises(SystemExit):
        _cfg(roles={}).validate()


def test_guard_response_tenant_refuses_corp_response():
    client = CaptureClient(_cfg())
    with pytest.raises(SystemExit):
        client._guard_response_tenant({"decision_record": {"tenant_id": CORP_TENANT}})


def test_guard_response_tenant_refuses_mismatch():
    client = CaptureClient(_cfg(tenant="dogfood-t"))
    with pytest.raises(SystemExit):
        client._guard_response_tenant({"decision_record": {"tenant_id": "some-other-tenant"}})


def test_guard_response_tenant_allows_match():
    client = CaptureClient(_cfg(tenant="dogfood-t"))
    client._guard_response_tenant({"decision_record": {"tenant_id": "dogfood-t"}})  # no raise


# ── record_decision: validation + payload shape (network monkeypatched) ──────

def test_record_decision_rejects_unknown_domain():
    client = CaptureClient(_cfg())
    with pytest.raises(ValueError):
        client.record_decision(work_item_id="w1", agent_handle="cc-builder@ulissy",
                               domain="not-a-domain", decision="certify")


def test_record_decision_rejects_bad_decision():
    client = CaptureClient(_cfg())
    with pytest.raises(ValueError):
        client.record_decision(work_item_id="w1", agent_handle="cc-builder@ulissy",
                               domain="deploy-verification", decision="maybe")


def test_record_decision_rejects_bad_verifiability_tag():
    client = CaptureClient(_cfg())
    with pytest.raises(ValueError):
        client.record_decision(work_item_id="w1", agent_handle="cc-builder@ulissy",
                               domain="deploy-verification", decision="certify",
                               verifiability_tag="guess")


def test_record_decision_rejects_unknown_role():
    client = CaptureClient(_cfg(roles={"cc-builder@ulissy": "aa" * 32}))
    with pytest.raises(ValueError):
        client.record_decision(work_item_id="w1", agent_handle="ghost@ulissy",
                               domain="deploy-verification", decision="certify")


def test_record_decision_injects_key_and_captures_domain(monkeypatch):
    client = CaptureClient(_cfg(roles={"cc-builder@ulissy": "bb" * 32}))
    seen = {}

    def fake_api(method, path, body=None):
        seen["method"], seen["path"], seen["body"] = method, path, body
        return 200, {"decision_record": {"decision_id": "d1", "tenant_id": "dogfood-t"}}

    monkeypatch.setattr(client, "_api", fake_api)
    out = client.record_decision(work_item_id="pr-42", agent_handle="cc-builder@ulissy",
                                 domain="deploy-verification", decision="certify",
                                 verifiability_tag="judgment", reason_text="looks safe")
    assert seen["path"] == "/v1/governed/decisions"
    b = seen["body"]
    # role key injected by the server, never taken from the caller
    assert b["agent_key"] == "bb" * 32
    assert b["agent_handle"] == "cc-builder@ulissy"
    assert b["verifiability_tag"] == "judgment"
    assert b["invoice_id"] == "pr-42"
    # domain sent as the DEDICATED durable field (→ server stores parameters.cgr_domain),
    # NOT hidden in the (encrypted) context. A human-readable trail rides `reason`.
    assert b["domain"] == "deploy-verification"
    assert "cgr_domain" not in b["context"]      # single source of truth is the durable field
    assert "[deploy-verification]" in b["reason"]
    assert out["recorded"] is True and out["decision_id"] == "d1"


def test_record_outcome_maps_and_posts(monkeypatch):
    client = CaptureClient(_cfg())
    seen = {}

    def fake_api(method, path, body=None):
        seen["path"], seen["body"] = path, body
        return 200, {"posted": "ok"}

    monkeypatch.setattr(client, "_api", fake_api)
    out = client.record_outcome(work_item_id="pr-42", result="deploy_succeeded")
    assert seen["path"] == "/v1/governed/outcomes"
    assert seen["body"]["invoice_ref"] == "pr-42"
    assert seen["body"]["outcome"] == "paid"
    assert out["posted"] is True and out["outcome"] == "paid"


def test_record_outcome_unmapped_is_noop(monkeypatch):
    client = CaptureClient(_cfg())
    called = {"n": 0}

    def fake_api(method, path, body=None):
        called["n"] += 1
        return 200, {}

    monkeypatch.setattr(client, "_api", fake_api)
    out = client.record_outcome(work_item_id="pr-42", result="still_running")
    assert out["posted"] is False
    assert called["n"] == 0  # never posts an unmapped outcome


def test_domains_are_the_locked_taxonomy():
    assert DEV_DOMAINS == ("deploy-verification", "security-scan", "adversarial-review")


# ── domain durability: it survives the CGR-readable loader → export projection ──
# (the field per-domain re-scoring reads; not a client-side log)

def test_cgr_domain_is_durable_in_substrate_export():
    from aml.cgr.substrate import DecisionRow, export_rows
    row = DecisionRow(
        decision_id="d1", invoice_ref="pr-42", agent_handle="cc-builder@ulissy",
        agent_tier=None, decision="certify", reason_code=None,
        verifiability_tag="judgment", created_at=None, outcome=None, outcome_date=None,
        agent_key="bb" * 32, cgr_domain="deploy-verification",
    )
    out = export_rows([row])[0]
    assert out["cgr_domain"] == "deploy-verification"          # surfaced, durable, CGR-readable
    assert list(out.keys())[-1] == "cgr_domain"               # appended (12th) — historical shape intact
    assert list(out.keys())[:10] == [                          # first 10 keys unchanged
        "decision_id", "invoice_ref", "agent_handle", "agent_tier", "decision",
        "reason_code", "verifiability_tag", "created_at", "outcome", "outcome_date"]
