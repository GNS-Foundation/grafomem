"""A1: UsageReporter.start() must perform NO DDL at runtime (cloud). usage_report_cursor
is provisioned by the pre-deploy release step (ensure_schema via _init), never by the
runtime process, which runs as the least-privilege role with no CREATE on schema public.

Fails BEFORE the fix (start() called ensure_schema) — watch it fail first.
"""
import asyncio
import pytest

from aml.cloud import usage_reporter as ur_mod
from aml.cloud.usage_reporter import UsageReporter


def test_start_performs_no_ddl_in_cloud(monkeypatch):
    monkeypatch.setattr(ur_mod, "metered_enabled", lambda: True)  # cloud/metered on
    r = UsageReporter("postgresql://unused", decision_trail=None, stripe_billing=None)

    ddl_calls = {"ensure_schema": 0}
    monkeypatch.setattr(r, "ensure_schema", lambda: ddl_calls.__setitem__("ensure_schema", ddl_calls["ensure_schema"] + 1))
    monkeypatch.setattr(r, "_cursor_table_exists", lambda: True)  # provisioned by the release step

    async def _noop_loop():
        return
    monkeypatch.setattr(r, "_loop", _noop_loop)  # don't spin the real periodic loop

    async def run():
        await r.start()
        await r.stop()
    asyncio.run(run())

    assert ddl_calls["ensure_schema"] == 0, (
        "start() ran ensure_schema — the runtime process must do NO DDL; "
        "usage_report_cursor is created by the ensure-schema release step (A1)"
    )


def test_start_declines_when_cursor_table_absent(monkeypatch):
    """No DDL, but still fail-closed: if the table was never provisioned, don't start."""
    monkeypatch.setattr(ur_mod, "metered_enabled", lambda: True)
    r = UsageReporter("postgresql://unused", decision_trail=None, stripe_billing=None)
    monkeypatch.setattr(r, "ensure_schema", lambda: (_ for _ in ()).throw(AssertionError("no DDL")))
    monkeypatch.setattr(r, "_cursor_table_exists", lambda: False)

    async def run():
        await r.start()
    asyncio.run(run())
    assert r._running is False, "must not start when usage_report_cursor is absent"
