"""gate B — encrypt decision-record context (PII) at rest.

Runs the REAL DecisionTrailService against Postgres (CI provides it; matches the #13
encrypted-Postgres test), with content encryption ENABLED — the prod config.

Covers the three claims the task requires:
  1. propose_action stores NO plaintext PII in the raw `query`/`raw_output` columns —
     the ciphertext lands in `query_enc`, and CGR-join fields survive in `parameters`.
  2. CGR still scores post-encryption: the encrypted decision joins to its outcome via
     load_substrate with agent_key/invoice_ref/agent_handle intact (CGR reads parameters,
     never query).
  3. The re-encryption migration is idempotent, skips already-encrypted rows, and never
     double-encrypts — verified across a FRESH connection (the pooled-connection commit
     lesson from the HITL bug).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import psycopg
import pytest
from cryptography.fernet import Fernet

from aml.backends.interface import WriteOptions
from aml.backends.postgres_gmp import PostgresGMPBackend
from aml.cgr.substrate import CGR_OUTCOMES_STORE, CGR_OUTCOME_SCHEMA, load_substrate
from aml.cloud.decision_trail import DecisionTrailService
from aml.cloud.orchestrator import AgentDefinition, OrchestratorService
from aml.cloud.tenant_key_manager import FernetEncryptor
from aml.server.stores import StoreManager
from aml.cloud.invoice_pseudonym import pseudonymize, is_pseudonymized
from ops.encrypt_decision_context import (
    ContentsParsedNotEmpty, count_plaintext, reencrypt_decision_context,
)

TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"
KEY = "b" * 64
_NOW = datetime(2026, 8, 7, tzinfo=timezone.utc)

# PII markers that appear ONLY inside the args/context (never in invoice_ref, which is a
# CGR join key that stays plaintext in `parameters` by design).
PII_COMPANY = "Contoso-Acme-Globex Ltd"
PII_EMAIL = "ana.persson@contoso-acme-globex.example"


def _enc():
    return FernetEncryptor(Fernet(Fernet.generate_key()))


def _dt():
    dt = DecisionTrailService(TEST_DB_URL)
    dt.ensure_schema()
    return dt


def _agent(tenant_id):
    return AgentDefinition(
        agent_id="aid", tenant_id=tenant_id, name="gtm-outreach-agent@ulissy", role="custom",
        description="", model_id="claude-3-opus-20240229", fallback_models=[],
        system_prompt="", memory_stores=[], tools=[], max_steps=20, max_tokens_per_step=4096,
        temperature=0.7, enabled=True, created_at=_NOW, updated_at=_NOW,
        agent_key=KEY, agent_handle="gtm-outreach-agent@ulissy",
    )


def _raw_row(decision_id):
    """Read the row as it sits AT REST (no decryption), over a fresh connection."""
    with psycopg.connect(TEST_DB_URL) as c:
        r = c.execute(
            "SELECT query, query_enc, raw_output, parameters "
            "FROM decision_records WHERE decision_id = %s",
            (decision_id,),
        ).fetchone()
    return {"query": r[0], "query_enc": r[1], "raw_output": r[2], "parameters": r[3]}


# ─────────────────────────────────────────────────────────────────────────────
# 1. propose_action encrypts the context — no plaintext PII at rest
# ─────────────────────────────────────────────────────────────────────────────

def test_propose_action_leaves_no_plaintext_pii(monkeypatch):
    T = "enc-" + uuid.uuid4().hex[:8]
    enc = _enc()
    dt = _dt()
    orch = OrchestratorService(db_url="", governance=None, decision_trail=dt,
                               signing_identity=None, encryption=enc)
    monkeypatch.setattr(orch, "get_agent", lambda aid, encryption=None: _agent(T))

    out = orch.propose_action(
        T, "aid", "send_email",
        {"to": PII_EMAIL, "subject": f"Q3 renewal — {PII_COMPANY}"},
        "OUT-0001",  # invoice_ref: carries NO PII (goes to parameters, stays plaintext)
        reason="fit: allocator",
    )
    did = out["decision_id"]
    row = _raw_row(did)

    # (a) the plaintext columns are the sentinel — the PII is NOT readable at rest
    assert row["query"] == "[ENCRYPTED]"
    assert row["query_enc"] is not None
    assert PII_COMPANY not in (row["query"] or "")
    assert PII_EMAIL not in (row["query"] or "")
    assert PII_COMPANY not in (row["raw_output"] or "")
    assert PII_EMAIL not in (row["raw_output"] or "")

    # (b) the ciphertext round-trips back to the real context (nothing lost)
    clear = enc.decrypt(row["query_enc"])
    assert PII_COMPANY in clear and PII_EMAIL in clear

    # (c) CGR join keys survive in parameters (JSONB, never encrypted)
    params = row["parameters"] if isinstance(row["parameters"], dict) else json.loads(row["parameters"])
    assert params["invoice_ref"] == pseudonymize("OUT-0001", T) and is_pseudonymized(params["invoice_ref"])
    assert params["agent_key"] == KEY
    assert params["agent_handle"] == "gtm-outreach-agent@ulissy"
    # invoice_ref (the only plaintext identifier) is intentional and PII-free
    assert PII_COMPANY not in json.dumps(params)


# ─────────────────────────────────────────────────────────────────────────────
# 2. CGR still scores post-encryption
# ─────────────────────────────────────────────────────────────────────────────

def test_cgr_join_survives_encryption(monkeypatch):
    T = "enc-" + uuid.uuid4().hex[:8]
    INV = f"{T}-INV1"
    enc = _enc()
    dt = _dt()
    orch = OrchestratorService(db_url="", governance=None, decision_trail=dt,
                               signing_identity=None, encryption=enc)
    monkeypatch.setattr(orch, "get_agent", lambda aid, encryption=None: _agent(T))

    orch.propose_action(T, "aid", "send_email",
                        {"to": PII_EMAIL, "subject": PII_COMPANY}, INV, reason="fit")

    sm = StoreManager(lambda: PostgresGMPBackend(TEST_DB_URL, encryption=enc))
    try:
        # before any outcome: the encrypted decision is visible with attribution intact
        rows = load_substrate(dt, sm, T)
        mine = [r for r in rows if r.invoice_ref == pseudonymize(INV, T)]
        assert len(mine) == 1, "encrypted decision must be visible to the CGR join"
        assert mine[0].agent_key == KEY
        assert mine[0].agent_handle == "gtm-outreach-agent@ulissy"
        assert mine[0].outcome is None

        # write the ground-truth outcome, then the join must resolve it (scorer input)
        ob = sm.get_or_create_named(CGR_OUTCOMES_STORE).backend
        meta = {"predicate": "receivable_outcome", "subject": pseudonymize(INV, T), "object": "paid",
                "cgr_schema": CGR_OUTCOME_SCHEMA, "source": "manual"}
        ob.write(f"receivable_outcome | {pseudonymize(INV, T)} | paid", WriteOptions(tenant_id=T, metadata=meta))

        rows2 = load_substrate(dt, sm, T)
        resolved = next(r for r in rows2 if r.invoice_ref == pseudonymize(INV, T))
        assert resolved.outcome == "paid", "encrypted decision must resolve against its outcome"
        assert resolved.agent_key == KEY
    finally:
        for entry in list(getattr(sm, "_stores", {}).values()):
            try:
                entry.backend.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# 3. Migration is idempotent, skips encrypted rows, never double-encrypts
# ─────────────────────────────────────────────────────────────────────────────

def _seed_plaintext(dt, T, marker):
    ctx = {"tool": "send_email", "args": {"to": PII_EMAIL, "co": marker}, "invoice_ref": "OUT-x"}
    rec = dt.log(
        tenant_id=T, store_id="governed",
        query=json.dumps(ctx, sort_keys=True),
        model_id="m", raw_output=json.dumps({"decision": "certify"}),
        parameters={"invoice_ref": "OUT-x", "agent_key": KEY,
                    "agent_handle": "a@b", "cgr_schema": "cgr.decision.v1"},
        # NO encryption → plaintext at rest (the pre-fix state we backfill)
    )
    return rec.decision_id


def _raw_row_full(decision_id):
    with psycopg.connect(TEST_DB_URL) as c:
        r = c.execute("SELECT query, query_enc, raw_output, raw_output_enc "
                      "FROM decision_records WHERE decision_id = %s", (decision_id,)).fetchone()
    return {"query": r[0], "query_enc": r[1], "raw_output": r[2], "raw_output_enc": r[3]}


def test_migration_encrypts_idempotently(monkeypatch, tmp_path):
    T = "enc-" + uuid.uuid4().hex[:8]
    enc = _enc()
    dt = _dt()
    backup = str(tmp_path / "preimage.jsonl")

    # two plaintext rows (pre-fix) + one already-encrypted row (must be skipped, untouched)
    d1 = _seed_plaintext(dt, T, PII_COMPANY)
    d2 = _seed_plaintext(dt, T, "Northwind Traders")
    pre_enc_ctx = {"tool": "send_email", "args": {"co": "AlreadySafe Inc"}}
    rec_enc = dt.log(tenant_id=T, store_id="governed",
                     query=json.dumps(pre_enc_ctx, sort_keys=True), model_id="m",
                     raw_output="{}", parameters={"invoice_ref": "OUT-y", "agent_key": KEY},
                     encryption=enc)                       # already encrypted at rest
    d3 = rec_enc.decision_id
    d3_enc_before = _raw_row(d3)["query_enc"]

    assert count_plaintext_via_conn(T) == 2                # d1, d2 plaintext; d3 is sentinel

    # ── run 1: encrypts d1, d2; skips d3 ──
    with psycopg.connect(TEST_DB_URL) as conn:
        stats1 = reencrypt_decision_context(conn, enc, T, backup_path=backup)
    assert stats1["encrypted_rows"] == 2
    assert stats1["scanned"] == 2

    # pre-image backup (#2) captured both rows' plaintext BEFORE mutation
    backup_lines = [json.loads(l) for l in open(backup) if l.strip()]
    assert {b["decision_id"] for b in backup_lines} == {d1, d2}
    assert any(PII_COMPANY in b["query"] for b in backup_lines)

    # verified over a FRESH connection (commit actually persisted — the HITL-bug lesson)
    for did, marker in ((d1, PII_COMPANY), (d2, "Northwind Traders")):
        row = _raw_row_full(did)
        assert row["query"] == "[ENCRYPTED]"
        assert row["raw_output"] == "[ENCRYPTED]"          # (#3) raw_output encrypted too
        assert marker not in (row["query"] or "")
        assert marker in enc.decrypt(row["query_enc"])     # round-trips to original PII
        assert "certify" in enc.decrypt(row["raw_output_enc"])
    # the pre-encrypted row is byte-for-byte untouched (NOT double-encrypted)
    assert _raw_row(d3)["query_enc"] == d3_enc_before
    assert "AlreadySafe Inc" in enc.decrypt(_raw_row(d3)["query_enc"])

    assert count_plaintext_via_conn(T) == 0

    # ── run 2: idempotent no-op ──
    with psycopg.connect(TEST_DB_URL) as conn:
        stats2 = reencrypt_decision_context(conn, enc, T, backup_path=backup)
    assert stats2["encrypted_rows"] == 0
    assert stats2["scanned"] == 0
    # still decrypts to the ORIGINAL (single encryption, not doubled)
    assert PII_COMPANY in enc.decrypt(_raw_row(d1)["query_enc"])


def test_migration_dry_run_writes_nothing():
    T = "enc-" + uuid.uuid4().hex[:8]
    enc = _enc()
    dt = _dt()
    _seed_plaintext(dt, T, PII_COMPANY)

    with psycopg.connect(TEST_DB_URL) as conn:
        stats = reencrypt_decision_context(conn, enc, T, dry_run=True)   # no backup needed
    assert stats["dry_run"] is True
    assert stats["scanned"] == 1
    assert stats["encrypted_rows"] == 0                    # NO writes on dry-run
    assert count_plaintext_via_conn(T) == 1                # still plaintext


def test_migration_aborts_on_nonempty_contents(tmp_path):
    # GUARD (#3): a row with retrieved_contents data must ABORT — the migration only encrypts
    # query/raw_output, so it must refuse rather than leave those columns plaintext.
    T = "enc-" + uuid.uuid4().hex[:8]
    enc = _enc()
    dt = _dt()
    dt.log(tenant_id=T, store_id="governed", query=json.dumps({"co": PII_COMPANY}),
           model_id="m", raw_output="{}", retrieved_contents=["a sensitive retrieved fact"],
           parameters={"invoice_ref": "OUT-z", "agent_key": KEY})       # NON-empty contents

    with psycopg.connect(TEST_DB_URL) as conn:
        with pytest.raises(ContentsParsedNotEmpty):
            reencrypt_decision_context(conn, enc, T, backup_path=str(tmp_path / "b.jsonl"))
    # nothing was mutated
    assert count_plaintext_via_conn(T) == 1


def test_migration_apply_requires_backup(tmp_path):
    T = "enc-" + uuid.uuid4().hex[:8]
    enc = _enc()
    dt = _dt()
    _seed_plaintext(dt, T, PII_COMPANY)
    with psycopg.connect(TEST_DB_URL) as conn:
        with pytest.raises(ValueError):
            reencrypt_decision_context(conn, enc, T)       # apply without backup_path → refuse
    assert count_plaintext_via_conn(T) == 1                # unmutated


# ─────────────────────────────────────────────────────────────────────────────
# 4. decision_routes POST /v1/decisions/log — the missed caller (Cowork #1)
# ─────────────────────────────────────────────────────────────────────────────

def test_decision_log_route_encrypts_at_rest():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from aml.cloud.decision_routes import create_decision_router

    T = "enc-" + uuid.uuid4().hex[:8]
    enc = _enc()
    dt = _dt()

    app = FastAPI()
    app.state.encryption = enc
    app.state.signing_identity = None

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.tenant = SimpleNamespace(tenant_id=T, scopes=["*"])
        return await call_next(request)

    app.include_router(create_decision_router(dt))
    client = TestClient(app)

    r = client.post("/v1/decisions/log", json={
        "store_id": "governed",
        "query": json.dumps({"company": PII_COMPANY, "to": PII_EMAIL}),
        "model_id": "m", "raw_output": json.dumps({"decision": "certify"}),
        "parameters": {"invoice_ref": "OUT-r", "agent_key": KEY},
    })
    assert r.status_code == 200, r.text
    did = r.json()["decision_id"]

    # at rest: encrypted, no plaintext PII in the raw column
    raw = _raw_row(did)
    assert raw["query"] == "[ENCRYPTED]"
    assert PII_COMPANY not in (raw["query"] or "")
    assert PII_COMPANY in enc.decrypt(raw["query_enc"])

    # authorized API read decrypts transparently (no read regression from the write fix)
    got = client.get(f"/v1/decisions/{did}")
    assert got.status_code == 200, got.text
    assert PII_COMPANY in got.json()["query"]


def test_count_plaintext_llm_providers_executes():
    # regression: LIKE 'gAAAAA%' crashed psycopg ("only %s allowed") in param-parsing mode;
    # the '%%' escape must let BOTH the tenant-scoped and global forms run.
    from ops.encrypt_decision_context import count_plaintext_llm_providers
    T = "enc-" + uuid.uuid4().hex[:8]
    with psycopg.connect(TEST_DB_URL) as c:
        assert count_plaintext_llm_providers(c, T) == 0            # tenant-scoped (params)
        assert isinstance(count_plaintext_llm_providers(c, None), int)  # global (empty params)


def count_plaintext_via_conn(tenant_id):
    with psycopg.connect(TEST_DB_URL) as c:
        return count_plaintext(c, tenant_id)
