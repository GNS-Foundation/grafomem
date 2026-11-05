"""0017 — hash-chained erasure ledger + a real erasure service in the MCP process.

Must-fail tests: each pins one guard and FAILS on pre-0017 code (no seq/hashes, dead
MCP certificate branch, HTTP-only verification). Flip the guard, watch it break, restore.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aml.cloud.erasure_ledger import (
    GENESIS_HASH, GENESIS_ENTRY_TYPE, ErasureLedger,
    canonical, chain_payload, entry_hash, genesis_hash, verify_ledger,
)

LEDGER_URL = os.environ.get("GRAFOMEM_LEDGER_URL")
MIGRATION = Path(__file__).resolve().parents[1] / "src/aml/cloud/migrations/021_erasure_ledger_chain.sql"


# ── hermetic: the verifier over plain row dicts ──────────────────────────────

def _row(seq, prev, entry_type="subject_erasure", **kw):
    r = {"entry_id": kw.get("entry_id", f"e{seq}"), "tenant_id": "t1", "entry_type": entry_type,
         "fact_ref": kw.get("fact_ref", seq), "content_hash": kw.get("content_hash"),
         "certificate": kw.get("certificate", {"certificate_id": f"c{seq}", "fact_ref": seq}),
         "backfill": None, "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seq),
         "seq": seq, "prev_hash": prev}
    r["entry_hash"] = entry_hash(prev, chain_payload(r))
    return r


def _chain(n_legacy=0, n_chained=3):
    rows = []
    for i in range(1, n_legacy + 1):
        rows.append({"entry_id": f"legacy{i}", "tenant_id": "t1", "entry_type": "subject_erasure",
                     "fact_ref": i, "content_hash": None, "certificate": None, "backfill": None,
                     "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=i),
                     "seq": i, "prev_hash": None, "entry_hash": None})
    g_seq = n_legacy + 1
    rows.append({"entry_id": "chain_genesis", "tenant_id": "__ledger__", "entry_type": GENESIS_ENTRY_TYPE,
                 "fact_ref": None, "content_hash": None, "certificate": None, "backfill": None,
                 "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc), "seq": g_seq,
                 "prev_hash": GENESIS_HASH, "entry_hash": genesis_hash([r["entry_id"] for r in rows])})
    prev = rows[-1]["entry_hash"]
    for i in range(1, n_chained + 1):
        r = _row(g_seq + i, prev)
        rows.append(r)
        prev = r["entry_hash"]
    return rows


def test_canonical_matches_the_adapter():
    # Same construction as adapters/langgraph-checkpoint-grafomem receipt_log.py.
    assert canonical({"b": 1, "a": [1, {"d": 2, "c": 3}]}) == '{"a":[1,{"c":3,"d":2}],"b":1}'
    assert GENESIS_HASH == "0" * 64


def test_verifier_accepts_a_good_chain_with_and_without_legacy():
    assert verify_ledger(_chain(0, 3)) == (True, [])
    assert verify_ledger(_chain(4, 3)) == (True, [])


def test_verifier_detects_dropped_and_reordered_rows():
    rows = _chain(0, 4)
    dropped = [r for r in rows if r["seq"] != 3]
    ok, problems = verify_ledger(dropped)
    assert not ok and any("seq not contiguous" in p for p in problems)

    # Re-sequence after a drop (what a careless "repair" would do): the link still breaks.
    for i, r in enumerate(dropped, 1):
        r["seq"] = i
    ok, problems = verify_ledger(dropped)
    assert not ok and any("does not link" in p for p in problems)

    swapped = _chain(0, 4)
    swapped[2], swapped[3] = swapped[3], swapped[2]
    swapped[2]["seq"], swapped[3]["seq"] = 3, 4
    ok, problems = verify_ledger(swapped)
    assert not ok


def test_verifier_detects_a_tampered_payload():
    rows = _chain(0, 3)
    rows[-1]["certificate"]["fact_ref"] = 999  # change content, keep the stored hash
    ok, problems = verify_ledger(rows)
    assert not ok and any("does not recompute" in p for p in problems)


def test_verifier_detects_a_removed_legacy_row_via_the_genesis_seal():
    rows = _chain(3, 2)
    rows = [r for r in rows if r["entry_id"] != "legacy2"]
    for i, r in enumerate(rows, 1):
        r["seq"] = i
    ok, problems = verify_ledger(rows)
    assert not ok and any("genesis hash mismatch" in p for p in problems)


def test_verifier_refuses_unsealed_or_empty():
    assert verify_ledger([]) == (False, ["ledger is empty or unsealed: no chain_genesis row"])
    legacy_only = _chain(2, 0)[:-1]  # legacy rows, genesis removed
    ok, problems = verify_ledger(legacy_only)
    assert not ok and any("chain_genesis" in p for p in problems)


# ── real Postgres: the ledger end to end ─────────────────────────────────────

@pytest.fixture
def ledger():
    if not LEDGER_URL:
        pytest.skip("GRAFOMEM_LEDGER_URL not set")
    import psycopg
    # A fresh, pre-021-shaped table every time (what a ledger looks like BEFORE 0017),
    # so ensure_schema() / the migration are exercised from the legacy state.
    with psycopg.connect(LEDGER_URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS erasure_ledger")
        c.execute("""CREATE TABLE erasure_ledger (
            entry_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, entry_type TEXT NOT NULL,
            fact_ref INTEGER, content_hash TEXT, certificate JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    el = ErasureLedger(LEDGER_URL)
    yield el
    el.close()


def _legacy_rows(n):
    import psycopg
    with psycopg.connect(LEDGER_URL, autocommit=True) as c:
        for i in range(1, n + 1):
            c.execute("INSERT INTO erasure_ledger (entry_id, tenant_id, entry_type, fact_ref, created_at) "
                      "VALUES (%s, 't1', 'subject_erasure', %s, now() - make_interval(secs => %s))",
                      (f"legacy-{i}", i, 100 - i))


def test_ensure_schema_seals_legacy_rows_and_chains_new_ones(ledger):
    _legacy_rows(3)
    ledger.ensure_schema()
    ledger.record_subject_erasure("c1", "t1", 10, "h1", {"certificate_id": "c1", "fact_ref": 10})
    ledger.record_tenant_destruction("c2", "t2", {"certificate_id": "c2", "action": "DESTROY_DEK"})

    rows = ledger.get_chain_rows()
    assert [r["seq"] for r in rows] == [1, 2, 3, 4, 5, 6]
    assert [r["entry_type"] for r in rows][3] == GENESIS_ENTRY_TYPE
    assert rows[3]["entry_hash"] == genesis_hash(["legacy-1", "legacy-2", "legacy-3"])
    assert ledger.verify() == (True, [])
    # restore-scrub never sees the genesis row
    assert all(r["entry_type"] != GENESIS_ENTRY_TYPE for r in ledger.get_all_erasures())


def test_ensure_schema_is_idempotent_and_matches_the_migration_file(ledger):
    import psycopg
    _legacy_rows(2)
    ledger.ensure_schema()
    ledger.ensure_schema()
    with psycopg.connect(LEDGER_URL, autocommit=True) as c:
        c.execute(MIGRATION.read_text(encoding="utf-8"))  # the shipped file, once more
        n_gen = c.execute("SELECT count(*) FROM erasure_ledger WHERE entry_type = %s",
                          (GENESIS_ENTRY_TYPE,)).fetchone()[0]
    assert n_gen == 1
    assert ledger.verify() == (True, [])


def test_empty_ledger_self_seals_on_first_write(ledger):
    import psycopg
    with psycopg.connect(LEDGER_URL, autocommit=True) as c:  # columns only, no genesis
        for col, typ in (("seq", "BIGINT"), ("prev_hash", "TEXT"), ("entry_hash", "TEXT"), ("backfill", "JSONB")):
            c.execute(f"ALTER TABLE erasure_ledger ADD COLUMN IF NOT EXISTS {col} {typ}")
    ledger.record_subject_erasure("c1", "t1", 1, None, {"certificate_id": "c1"})
    rows = ledger.get_chain_rows()
    assert [r["entry_type"] for r in rows] == [GENESIS_ENTRY_TYPE, "subject_erasure"]
    assert rows[0]["entry_hash"] == genesis_hash([])
    assert ledger.verify() == (True, [])


def test_unsealed_legacy_rows_refuse_writes(ledger):
    import psycopg
    _legacy_rows(2)
    with psycopg.connect(LEDGER_URL, autocommit=True) as c:  # columns, backfilled seq, NO genesis
        for col, typ in (("seq", "BIGINT"), ("prev_hash", "TEXT"), ("entry_hash", "TEXT"), ("backfill", "JSONB")):
            c.execute(f"ALTER TABLE erasure_ledger ADD COLUMN IF NOT EXISTS {col} {typ}")
        c.execute("UPDATE erasure_ledger SET seq = fact_ref")
    with pytest.raises(RuntimeError, match="unsealed"):
        ledger.record_subject_erasure("c1", "t1", 9, None, {"certificate_id": "c1"})


def test_concurrent_writers_keep_seq_contiguous_and_chain_valid(ledger):
    ledger.ensure_schema()
    errors: list[Exception] = []

    def writer(tag):
        try:
            for i in range(10):
                ledger.record_subject_erasure(f"{tag}-{i}", "t1", i, None, {"certificate_id": f"{tag}-{i}"})
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(f"w{k}",)) for k in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors
    rows = ledger.get_chain_rows()
    assert len(rows) == 1 + 40
    assert [r["seq"] for r in rows] == list(range(1, 42))
    assert ledger.verify() == (True, [])


def test_superuser_deleting_a_row_is_detected(ledger):
    import psycopg
    ledger.ensure_schema()
    for i in range(5):
        ledger.record_subject_erasure(f"c{i}", "t1", i, None, {"certificate_id": f"c{i}"})
    assert ledger.verify() == (True, [])
    with psycopg.connect(LEDGER_URL, autocommit=True) as c:
        c.execute("DELETE FROM erasure_ledger WHERE entry_id = 'c2'")
    ok, problems = ledger.verify()
    assert not ok
    assert any("seq not contiguous" in p for p in problems) or any("does not link" in p for p in problems)


def test_certificate_payload_round_trips_through_jsonb(ledger):
    """The verifier rebuilds the payload from JSONB. Our certificate payloads are
    str/int/bool/None/dict — they must hash identically after the round trip."""
    ledger.ensure_schema()
    cert = {"certificate_id": "c1", "tenant_id": "t1", "fact_ref": 7,
            "fact_content_hash": "ab" * 16, "coverage": {"primary": "absent", "embedding": "unverified"},
            "governance_record": {"declared": {"obligation": "deletion"}, "non_claims": {"coverage_gaps": ["embedding"]}},
            "erasure_requested_at": "2026-11-04T08:00:00.000000+00:00", "legal_basis": "GDPR Article 17 — Right to Erasure"}
    ledger.record_subject_erasure("c1", "t1", 7, "ab" * 16, cert)
    assert ledger.verify() == (True, [])


def test_migration_passes_the_split_role_gate():
    from aml.cloud.migrations_runner import validate_migration_sql
    validate_migration_sql(MIGRATION.name, MIGRATION.read_text(encoding="utf-8"), "grafomem_rt")


# ── MCP wiring (hermetic) ─────────────────────────────────────────────────────

from aml.backends.interface import Capability  # noqa: E402
from aml.server.mcp import create_mcp_server  # noqa: E402
from tests.test_mcp_governance import MockBackend, _FakeEPS  # noqa: E402


def _server(eps):
    def factory():
        return MockBackend([Capability.HARD_DELETE, Capability.MULTI_TENANT], behavior="honest")
    return create_mcp_server(factory, erasure_service=eps)


@pytest.mark.asyncio
async def test_mcp_without_service_says_so_and_fakes_nothing():
    server = _server(None)
    ref = json.loads((await server._test_call_tool("write_memory", {"content": "x"}))[0].text)["ref"]
    d = json.loads((await server._test_call_tool("delete_memory", {"ref": ref}))[0].text)
    assert d["deleted"] is True
    assert d["certificate"] is None
    assert d["certificate_status"] == "not_issued: no erasure service in this MCP process"
    v = json.loads((await server._test_call_tool("verify_erasure", {"ref": ref, "reverify": True, "probe_query": "x"}))[0].text)
    assert v["status"] == "NOT_FOUND"  # gone, no certificate, no row — nothing is claimed


@pytest.mark.asyncio
async def test_mcp_fact_still_present_is_reported_not_certified():
    from aml.cloud.erasure_proof import FactStillPresent

    class _Refusing(_FakeEPS):
        def issue_certificate(self, **kw):
            raise FactStillPresent("refuse: primary store still holds memory_ref:1")

    server = _server(_Refusing())
    ref = json.loads((await server._test_call_tool("write_memory", {"content": "x"}))[0].text)["ref"]
    d = json.loads((await server._test_call_tool("delete_memory", {"ref": ref}))[0].text)
    assert d["certificate"] is None
    assert d["certificate_status"] == "not_issued: fact_still_present"


# ── restore-scrub: verifies first, reports, still replays ────────────────────

def test_restore_scrub_reports_a_broken_chain_and_still_replays():
    from types import SimpleNamespace
    from aml.cloud.restore_scrub import reapply_ledger

    class _Ledger:
        def verify(self): return False, ["seq 3: prev_hash does not link to seq 2"]
        def get_all_erasures(self):
            return [{"tenant_id": "t1", "entry_type": "chain_genesis"},          # must be skipped
                    {"tenant_id": "t1", "entry_type": "tenant_destruction"}]

    destroyed = []
    tkm = SimpleNamespace(destroy_tenant_key=lambda t: destroyed.append(t), get_encryptor=lambda t: None)
    stats = reapply_ledger(_Ledger(), SimpleNamespace(_factory=lambda: None), tkm, SimpleNamespace())
    assert stats["ledger_chain_ok"] is False
    assert stats["ledger_chain_problems"] == ["seq 3: prev_hash does not link to seq 2"]
    assert destroyed == ["t1"]                 # replay still happened (warn, don't refuse)
    assert stats["tenant_keys_destroyed"] == 1  # and the genesis row was not "replayed"
