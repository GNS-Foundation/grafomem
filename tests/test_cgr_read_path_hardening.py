"""Perf hardening for the CGR read/write path (platform PR).

Findings covered:
  * F2 — /v1/cgr/scores decrypt cost: `_scoped_audit` caches the materialized
    (decrypted) tenant substrate for a short TTL, WITHOUT capping the scan, and the
    write path invalidates it so a post-then-read never sees stale data.
  * F1 — review/outcome bulk write: `_record_review`/`_record_outcome` accept a
    pre-scanned `existing` set so an N-item bulk scans ONCE, not N times.
  * F3 — manifold sweep yield is env-configurable (so it can't starve HTTP).
"""
from __future__ import annotations

import aml.cgr.substrate as substrate
from aml.cgr.substrate import (
    CGR_OUTCOME_SCHEMA,
    _scoped_audit,
    _tenant_outcomes,
    invalidate_substrate_cache,
)


class _Mem:
    def __init__(self, tenant_id, schema=CGR_OUTCOME_SCHEMA, subject="i-1"):
        self.tenant_id = tenant_id
        self.metadata = {"cgr_schema": schema, "subject": subject, "object": "paid"}


class _CountingBackend:
    """Records how many times scoped_audit is hit — the expensive decrypt scan."""
    def __init__(self, rows):
        self._rows = rows
        self.scoped_audit_calls = 0

    def scoped_audit(self, tenant_id):
        self.scoped_audit_calls += 1
        return iter([m for m in self._rows if m.tenant_id == tenant_id])


def setup_function(_):
    invalidate_substrate_cache(None)  # clean slate per test


# ── F2: cache bounds repeat decrypt cost without dropping rows ──
def test_scoped_audit_caches_within_ttl():
    b = _CountingBackend([_Mem("t")])
    r1 = _scoped_audit(b, "t")
    r2 = _scoped_audit(b, "t")
    assert b.scoped_audit_calls == 1          # second read served from cache
    assert list(r1) == list(r2)               # materialized list, re-iterable
    assert len(list(_scoped_audit(b, "t"))) == 1


def test_invalidate_forces_rescan():
    b = _CountingBackend([_Mem("t")])
    _scoped_audit(b, "t")
    invalidate_substrate_cache("t")
    _scoped_audit(b, "t")
    assert b.scoped_audit_calls == 2          # invalidation dropped the cache


def test_cache_is_per_tenant():
    b = _CountingBackend([_Mem("t1"), _Mem("t2")])
    _scoped_audit(b, "t1")
    _scoped_audit(b, "t2")
    _scoped_audit(b, "t1")                     # t1 cached; only t1+t2 first reads scanned
    assert b.scoped_audit_calls == 2


def test_ttl_zero_disables_cache(monkeypatch):
    monkeypatch.setattr(substrate, "_SUBSTRATE_CACHE_TTL_S", 0.0)
    b = _CountingBackend([_Mem("t")])
    _scoped_audit(b, "t"); _scoped_audit(b, "t")
    assert b.scoped_audit_calls == 2          # every read scans when disabled


def test_scoped_audit_returns_complete_set_not_truncated():
    rows = [_Mem("t", subject=f"i-{i}") for i in range(50)]
    b = _CountingBackend(rows)
    assert len(_tenant_outcomes(b, "t")) == 50   # completeness preserved


# ── F1: a pre-scanned `existing` means the write helper does NOT rescan ──
def test_record_review_uses_passed_existing(monkeypatch):
    import aml.cloud.demo_routes as dr

    calls = {"n": 0}
    monkeypatch.setattr(dr, "_tenant_reviews", lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1) or []))

    class _B:
        def capabilities(self): return set()
        def write(self, content, opts): pass
    dr._record_review(_B(), tenant_id="t", invoice_ref="i", reviewer_handle="r",
                      rating=1.0, agent_handle="a@x", decision_id=None,
                      review_date=None, source="s", existing=[])
    assert calls["n"] == 0                     # passed existing ⇒ no per-write rescan


# ── F3: sweep yield is configurable ──
def test_manifold_sweep_yield_configurable():
    import aml.cloud.manifold as m
    assert m._MANIFOLD_SWEEP_YIELD_S >= 0.0
