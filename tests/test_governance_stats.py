"""1B-5 — governance stats: real denials/escalations + evaluations_by_type.

The response-model declared denials_total/escalations_total but the gateway spread
evaluations_denied/escalated (wrong names) → they were pinned at 0. And there was no
evaluations_by_type. These tests lock the name-mapping and the full-aggregate
(top-N + Other, no undercount) grouping.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from aml.cloud.governance import GovernanceGateway

TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"


def _tenant() -> str:
    return f"gov-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def gw():
    g = GovernanceGateway(TEST_DB_URL)
    g.ensure_schema()
    g._evidence.ensure_schema()
    return g


def _seed(rows: list[tuple[str, str]], tenant: str) -> None:
    """rows: (policy_name, result). Insert eval-log entries directly."""
    with psycopg.connect(TEST_DB_URL, autocommit=True) as c:
        for pn, res in rows:
            c.execute(
                "INSERT INTO governance_evaluation_log "
                "(log_id, tenant_id, policy_id, policy_name, result, operation, detail, request_summary) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (uuid.uuid4().hex[:24], tenant, "pol-" + pn, pn, res, "op", "", ""),
            )


def test_denials_escalations_carry_real_values(gw):
    t = _tenant()
    _seed([("p_rate", "denied")] * 5 + [("p_rate", "escalated")] * 3 + [("p_pii", "allowed")] * 2, t)
    s = gw.get_stats(t)
    assert s["evaluations_total"] == 10
    assert s["denials_total"] == 5          # was structurally 0 before the fix
    assert s["escalations_total"] == 3


def test_evaluations_by_type_full_aggregate_no_undercount(gw):
    t = _tenant()
    rows = []
    for k in range(10):                      # 10 distinct policies, counts 1..10
        rows += [(f"policy_{k:02d}", "allowed")] * (k + 1)
    _seed(rows, t)
    total = sum(k + 1 for k in range(10))    # 55
    bt = gw.get_stats(t)["evaluations_by_type"]
    assert sum(bt.values()) == total         # tail folded into Other → no undercount
    assert "Other" in bt                     # >8 distinct → Other bucket
    assert len(bt) == 9                       # top-8 + Other
    assert bt["policy_09"] == 10             # busiest policy kept
    assert bt["Other"] == 1 + 2              # the two smallest (policy_00, policy_01) folded


def test_by_type_no_other_when_few_policies(gw):
    t = _tenant()
    _seed([("a", "allowed"), ("a", "allowed"), ("b", "denied")], t)
    assert gw.get_stats(t)["evaluations_by_type"] == {"a": 2, "b": 1}   # no Other


def test_empty_tenant(gw):
    s = gw.get_stats(_tenant())
    assert s["evaluations_total"] == 0 and s["denials_total"] == 0 and s["escalations_total"] == 0
    assert s["evaluations_by_type"] == {}


def test_response_model_preserves_new_fields():
    from aml.cloud.schemas import GovernanceStatsResponse
    d = GovernanceStatsResponse(
        policies_total=1, policies_active=1, evaluations_total=10,
        denials_total=5, escalations_total=3, evaluations_by_type={"a": 7, "Other": 3},
    ).model_dump()
    assert d["denials_total"] == 5 and d["escalations_total"] == 3
    assert d["evaluations_by_type"] == {"a": 7, "Other": 3}
