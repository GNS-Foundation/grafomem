"""Unit tests for the grafomem-cgr capture MCP server (Track C, ticket 1).

Pure-logic coverage — no network, no DB. The integration/full-loop acceptance
(decision → outcome → score movement) runs against the dogfood tenant via
`python ops/cgr_capture_mcp.py selftest` and is captured in the PR.
"""
from __future__ import annotations

import json
import os

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


# ── tenant guard: pin + denylist (invariant 1) ───────────────────────────────────────────

def test_assert_not_corp_refuses_corp():
    with pytest.raises(SystemExit):
        _assert_not_corp(CORP_TENANT, where="test")


def test_assert_not_corp_allows_other():
    _assert_not_corp("some-dogfood-tenant", where="test")  # no raise


def _cfg(tenant="dogfood-t", roles=None, forbidden=None):
    c = Config.__new__(Config)
    c.base_url = "https://api.grafomem.com"
    c.tenant_key = "k"
    c.expected_tenant = tenant
    # ops/ always denies corp (see ops.cgr_capture_mcp.main); mirror that policy here.
    c.forbidden_tenants = {CORP_TENANT} if forbidden is None else set(forbidden)
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

# ── durability guard: selftest proves the DEPLOYED API round-tripped cgr_domain ──

def _client_with_export(monkeypatch, export_body, export_code=200):
    client = CaptureClient(_cfg())

    def fake_api(method, path, body=None):
        if path == "/v1/cgr/substrate/export":
            return export_code, export_body
        return 200, {}
    monkeypatch.setattr(client, "_api", fake_api)
    return client


def test_verify_domain_durable_passes_when_echoed(monkeypatch):
    client = _client_with_export(monkeypatch, {"decisions": [
        {"decision_id": "d1", "cgr_domain": "deploy-verification"}]})
    out = client.verify_domain_durable("d1", "deploy-verification")
    assert out["durable"] is True and out["cgr_domain"] == "deploy-verification"


def test_verify_domain_durable_fails_on_silent_drop(monkeypatch):
    # deployed API predates the change → Pydantic dropped `domain` → row has no cgr_domain
    client = _client_with_export(monkeypatch, {"decisions": [
        {"decision_id": "d1", "cgr_domain": None}]})
    with pytest.raises(SystemExit):
        client.verify_domain_durable("d1", "deploy-verification")


def test_verify_domain_durable_fails_when_row_missing(monkeypatch):
    client = _client_with_export(monkeypatch, {"decisions": []})
    with pytest.raises(SystemExit):
        client.verify_domain_durable("d1", "deploy-verification")


def test_verify_domain_durable_fails_when_export_unreadable(monkeypatch):
    client = _client_with_export(monkeypatch, {"error": "forbidden"}, export_code=403)
    with pytest.raises(SystemExit):
        client.verify_domain_durable("d1", "deploy-verification")


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


# ── packaging generalization: pin + denylist, env-only role keys ──────────────
# (the guard that was hardcoded to GRAFOMEM's corp tenant now generalizes to any
# tenant, with ops/ supplying corp as its own denylist entry)

def test_forbidden_denylist_generalizes_beyond_corp():
    from ops.cgr_capture_mcp import _assert_not_forbidden
    with pytest.raises(SystemExit):
        _assert_not_forbidden("someone-elses-prod", {"someone-elses-prod"}, where="test")
    _assert_not_forbidden("capture-tenant", {"someone-elses-prod"}, where="test")  # no raise


def test_ops_entry_point_always_denies_corp(monkeypatch):
    """ops/ must inject corp into the denylist even if the env sets something else."""
    import ops.cgr_capture_mcp as ops_mcp

    monkeypatch.setenv("GRAFOMEM_CGR_FORBIDDEN_TENANTS", "other-tenant")
    seen = {}
    monkeypatch.setattr(ops_mcp, "_pkg_main", lambda argv: seen.update(
        env=os.environ["GRAFOMEM_CGR_FORBIDDEN_TENANTS"]))
    ops_mcp.main([])
    assert CORP_TENANT in seen["env"].split(",")
    assert "other-tenant" in seen["env"].split(",")


def test_guard_refuses_tenant_not_pinned_even_if_not_denylisted():
    """The pin is the primary guard: an unexpected tenant is refused on its own."""
    client = CaptureClient(_cfg(tenant="capture-t", forbidden=set()))
    with pytest.raises(SystemExit):
        client._guard_response_tenant({"decision_record": {"tenant_id": "unexpected-t"}})


def test_role_keys_can_come_from_inline_env_json(monkeypatch):
    """A stranger's config is env vars only — no role-keys file on disk."""
    monkeypatch.setenv("GRAFOMEM_CGR_TENANT_KEY", "k")
    monkeypatch.setenv("GRAFOMEM_CGR_TENANT", "capture-t")
    monkeypatch.setenv("GRAFOMEM_CGR_ROLE_KEYS_JSON", json.dumps({"cc-builder@acme": "cc" * 32}))
    monkeypatch.delenv("GRAFOMEM_CGR_ROLE_KEYS", raising=False)
    monkeypatch.delenv("GRAFOMEM_CGR_FORBIDDEN_TENANTS", raising=False)
    cfg = Config()
    cfg.validate()  # no raise
    assert cfg.role_keys == {"cc-builder@acme": "cc" * 32}
    assert cfg.expected_tenant == "capture-t"


def test_legacy_dogfood_tenant_env_still_honoured(monkeypatch):
    monkeypatch.delenv("GRAFOMEM_CGR_TENANT", raising=False)
    monkeypatch.setenv("GRAFOMEM_CGR_DOGFOOD_TENANT", "legacy-t")
    assert Config().expected_tenant == "legacy-t"


def test_config_missing_tenant_pin_fails_closed(monkeypatch):
    monkeypatch.setenv("GRAFOMEM_CGR_TENANT_KEY", "k")
    monkeypatch.delenv("GRAFOMEM_CGR_TENANT", raising=False)
    monkeypatch.delenv("GRAFOMEM_CGR_DOGFOOD_TENANT", raising=False)
    monkeypatch.setenv("GRAFOMEM_CGR_ROLE_KEYS_JSON", json.dumps({"a@b": "aa" * 32}))
    with pytest.raises(SystemExit):
        Config().validate()
