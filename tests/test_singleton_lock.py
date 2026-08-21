"""Cross-replica singleton advisory lock (Go-live Stage 1, Task 1).

The critical property: the lock is held on ONE dedicated connection for the ENTIRE cycle
body (not just around the lock primitive), so a second replica running the same cycle is
excluded for the whole run. These tests exercise the lock DURING the real
``run_once`` / ``refresh_once`` bodies — a competing acquire from a separate session must
fail mid-body and succeed only after the cycle ends.

Skips when no Postgres is configured (``GRAFOMEM_DB_URL``); CI provides one.
"""
import os

import psycopg
import pytest

DB = os.environ.get("GRAFOMEM_DB_URL")
pytestmark = pytest.mark.skipif(not DB, reason="needs GRAFOMEM_DB_URL (Postgres)")

from aml.cloud.singleton import (  # noqa: E402
    _LOCK_NS,
    LOCK_ID_FREE_CEILING_REFRESH,
    LOCK_ID_USAGE_REPORTER,
    cycle_singleton,
)


def _competing_acquire(lock_id: int) -> bool:
    """Try to grab the lock from a SEPARATE session; release immediately (close frees it)."""
    c = psycopg.connect(DB, autocommit=True)
    try:
        return bool(c.execute("SELECT pg_try_advisory_lock(%s, %s)", (_LOCK_NS, lock_id)).fetchone()[0])
    finally:
        c.close()


# ── primitive ────────────────────────────────────────────────────────────────

def test_mutual_exclusion_same_key():
    with cycle_singleton(DB, 91, "A") as a:
        assert a is True
        with cycle_singleton(DB, 91, "B") as b:
            assert b is False           # second cannot win while first holds it
    with cycle_singleton(DB, 91, "C") as c:
        assert c is True                # released on exit → reacquire wins


def test_distinct_keys_do_not_block():
    with cycle_singleton(DB, 92, "rep") as x, cycle_singleton(DB, 93, "ref") as y:
        assert x is True and y is True


def test_lock_released_on_crash():
    crash = psycopg.connect(DB, autocommit=True)
    assert crash.execute("SELECT pg_try_advisory_lock(%s,%s)", (_LOCK_NS, 94)).fetchone()[0] is True
    crash.close()                       # simulate process crash — session end frees the lock
    with cycle_singleton(DB, 94, "post-crash") as pc:
        assert pc is True


# ── the hold spans the REAL cycle body (the reviewer's requirement) ──────────

def test_lock_held_across_run_once_body(monkeypatch):
    """A competing acquire must FAIL while run_once's body executes, and SUCCEED after."""
    import aml.cloud.usage_reporter as ur
    for k, v in {"STRIPE_METER_ID": "m", "STRIPE_BASE_PRICE_ID": "b", "STRIPE_OVERAGE_PRICE_ID": "o"}.items():
        monkeypatch.setenv(k, v)

    r = ur.UsageReporter(DB, decision_trail=None, stripe_billing=None)
    monkeypatch.setattr(r, "_active_tenants", lambda: ["t1"])
    probe = {}

    def report(tid):
        # runs INSIDE run_once's cycle_singleton block → the lock must be held here
        probe["during"] = _competing_acquire(LOCK_ID_USAGE_REPORTER)
        return {"tenant_id": tid}

    monkeypatch.setattr(r, "report_tenant", report)
    r.run_once()
    assert probe["during"] is False, "lock was NOT held during the run_once body"
    assert _competing_acquire(LOCK_ID_USAGE_REPORTER) is True, "lock not released after the cycle"


def test_lock_held_across_refresh_body(monkeypatch):
    """Same guarantee for the 3c refresh cycle."""
    import aml.cloud.free_ceiling as fc
    monkeypatch.setenv("FREE_CEILING_ENABLED", "1")

    svc = fc.FreeCeilingService(DB, decision_trail=None, tenant_manager=None)
    probe = {}

    def body():
        probe["during"] = _competing_acquire(LOCK_ID_FREE_CEILING_REFRESH)
        return 0

    monkeypatch.setattr(svc, "_refresh_body", body)
    svc.refresh_once()
    assert probe["during"] is False, "lock was NOT held during the refresh body"
    assert _competing_acquire(LOCK_ID_FREE_CEILING_REFRESH) is True, "lock not released after the cycle"
