"""B6: SIEM export and retention are OFF by default and separately gated.

SIEM_EXPORT_ENABLED ("1"/"true" only) gates the export job: when off, the daemon does not schedule
`siem_export_job` and `SiemExporter.run_sweep` returns before the URL check and before any
`psycopg.connect`. Retention is split out of the export sweep: `_apply_retention_policy` runs only from
`run_retention_sweep`, scheduled as `siem_retention_job` ONLY when SIEM_RETENTION_ENABLED is "1"/"true";
an export never deletes anything. MUST-FAIL on main ae05ef6: there the export job is always scheduled
(erasure_daemon.py:79-87), run_sweep connects whenever a URL is set (siem_exporter.py:22-29), and the
same sweep deletes exported rows older than LOG_RETENTION_DAYS (siem_exporter.py:35-36).
"""
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import psycopg
import pytest

from aml.cloud import erasure_daemon
from aml.cloud.siem_exporter import SiemExporter

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
FLAGS = ("SIEM_EXPORT_ENABLED", "SIEM_RETENTION_ENABLED", "SIEM_WEBHOOK_URL", "LOG_RETENTION_DAYS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in FLAGS:
        monkeypatch.delenv(k, raising=False)


def _jobs(monkeypatch) -> set[str]:
    """Start the daemon scheduler with the sweep bodies stubbed out; return the scheduled job ids."""
    monkeypatch.setattr(erasure_daemon, "run_sweep_job", MagicMock(name="run_sweep_job"))
    monkeypatch.setattr(erasure_daemon, "run_siem_export_job", MagicMock(name="run_siem_export_job"))
    if hasattr(erasure_daemon, "run_siem_retention_job"):
        monkeypatch.setattr(erasure_daemon, "run_siem_retention_job", MagicMock(name="run_siem_retention_job"))
    sched = erasure_daemon.start_daemon("postgresql://unused/unused", interval_minutes=60)
    try:
        return {j.id for j in sched.get_jobs()}
    finally:
        sched.shutdown(wait=False)


# ---------------------------------------------------------------- 1. nothing scheduled when off

@pytest.mark.parametrize("value", [None, "0", "false", "yes", "on", ""])
def test_export_job_not_scheduled_when_flag_off_or_not_strictly_on(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("SIEM_EXPORT_ENABLED", value)
    monkeypatch.setenv("SIEM_WEBHOOK_URL", "http://siem.invalid/hook")  # a URL alone must not enable it
    jobs = _jobs(monkeypatch)
    assert "erasure_sweeper_job" in jobs, "the erasure sweeper is unaffected"
    assert "siem_export_job" not in jobs, jobs
    assert "siem_retention_job" not in jobs, jobs


def test_export_job_scheduled_only_with_strict_on(monkeypatch):
    for value in ("1", "true"):
        monkeypatch.setenv("SIEM_EXPORT_ENABLED", value)
        assert "siem_export_job" in _jobs(monkeypatch)


# ---------------------------------------------------------------- 2. URL set, flag off: no DB access

def test_run_sweep_with_url_but_flag_off_never_connects(monkeypatch):
    monkeypatch.setenv("SIEM_WEBHOOK_URL", "http://siem.invalid/hook")
    calls: list = []

    def _connect(*a, **k):
        calls.append(a)
        raise RuntimeError("must not be reached")

    monkeypatch.setattr(psycopg, "connect", _connect)
    SiemExporter("postgresql://unused/unused").run_sweep()   # run_sweep swallows errors: count the calls
    assert calls == [], "run_sweep reached psycopg.connect with SIEM_EXPORT_ENABLED off"


# ---------------------------------------------------------------- 3. export never deletes

@pytest.fixture
def old_exported_row():
    """One decision_records row 200 days old whose export cursor already lies beyond it (so retention,
    if it ran, WOULD delete it). Mirrors tests/test_siem_exporter.py's fixture shape."""
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS siem_export_cursors (
                table_name VARCHAR(255) PRIMARY KEY,
                last_exported_time TIMESTAMPTZ DEFAULT '1970-01-01 00:00:00+00',
                last_exported_ref TEXT DEFAULT '',
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("DELETE FROM decision_records WHERE decision_id = 'b6-old'")
        c.execute("""
            INSERT INTO decision_records (decision_id, tenant_id, store_id, created_at, query, retrieved_refs, model_id, raw_output)
            VALUES ('b6-old', 'b6', 's1', %s, 'q', '[]', 'm', 'o')""",
                  (datetime.now(timezone.utc) - timedelta(days=200),))
        c.execute("DELETE FROM siem_export_cursors WHERE table_name IN ('decision_records', 'audit_logs', 'gcrumbs_breadcrumbs')")
        c.execute("INSERT INTO siem_export_cursors (table_name, last_exported_time, last_exported_ref) VALUES "
                  "('decision_records', %s, 'zzz'), ('audit_logs', %s, 'zzz'), ('gcrumbs_breadcrumbs', %s, 'zzz')",
                  (datetime.now(timezone.utc),) * 3)
    yield
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("DELETE FROM decision_records WHERE decision_id = 'b6-old'")


def _row_exists() -> bool:
    with psycopg.connect(DB_URL) as c:
        return c.execute("SELECT 1 FROM decision_records WHERE decision_id = 'b6-old'").fetchone() is not None


def test_export_sweep_never_deletes(monkeypatch, old_exported_row):
    monkeypatch.setenv("SIEM_EXPORT_ENABLED", "1")
    monkeypatch.setenv("SIEM_WEBHOOK_URL", "http://siem.invalid/hook")
    monkeypatch.setenv("LOG_RETENTION_DAYS", "180")
    ok = MagicMock(status_code=200); ok.raise_for_status = MagicMock()
    with patch("aml.cloud.siem_exporter.httpx.post", return_value=ok):
        SiemExporter(DB_URL).run_sweep()
    assert _row_exists(), "the export sweep deleted an exported row older than LOG_RETENTION_DAYS"


def test_retention_sweep_deletes_only_when_run_explicitly(monkeypatch, old_exported_row):
    """The split-out retention sweep still prunes exported rows past the cutoff (the cursor guard kept)."""
    monkeypatch.setenv("LOG_RETENTION_DAYS", "180")
    SiemExporter(DB_URL).run_retention_sweep()
    assert not _row_exists()


# ---------------------------------------------------------------- 4. retention job is its own flag

def test_retention_job_absent_with_export_only(monkeypatch):
    monkeypatch.setenv("SIEM_EXPORT_ENABLED", "1")
    jobs = _jobs(monkeypatch)
    assert "siem_export_job" in jobs and "siem_retention_job" not in jobs, jobs


def test_retention_job_present_only_with_its_own_flag(monkeypatch):
    monkeypatch.setenv("SIEM_RETENTION_ENABLED", "true")
    jobs = _jobs(monkeypatch)
    assert "siem_retention_job" in jobs, jobs
    assert "siem_export_job" not in jobs, "retention on does not switch export on"
