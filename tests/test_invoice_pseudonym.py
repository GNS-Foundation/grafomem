"""invoice_ref pseudonymization — transform properties + the MUST-FIX join-integrity proof.

The join-integrity proof (CGR still scores a pseudonymized decision) runs the real
DecisionTrailService + CGR load_substrate on Postgres: a decision whose parameters.invoice_ref
is pseudonymized joins to an outcome whose subject is pseudonymized with the SAME per-tenant
key, and resolves — proving the pure-equality join survives the transform.
"""
from __future__ import annotations

import json
import uuid

import psycopg
import pytest
from cryptography.fernet import Fernet

from aml.backends.interface import WriteOptions
from aml.backends.postgres_gmp import PostgresGMPBackend
from aml.cgr.substrate import CGR_OUTCOMES_STORE, CGR_OUTCOME_SCHEMA, load_substrate
from aml.cloud.decision_trail import DecisionTrailService
from aml.cloud.invoice_pseudonym import pseudonymize, is_pseudonymized
from aml.cloud.tenant_key_manager import FernetEncryptor
from aml.server.stores import StoreManager

TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"
MK = "a" * 64                       # test master key (32 bytes hex)
RAW = "OUT-fasanara-capital-elisa-bianchi"


# ── transform properties ─────────────────────────────────────────────────────

def test_deterministic_per_tenant_idempotent_prefix():
    T1, T2 = "ten-A", "ten-B"
    p1 = pseudonymize(RAW, T1, master_key_hex=MK)
    # deterministic
    assert p1 == pseudonymize(RAW, T1, master_key_hex=MK)
    # per-tenant: same ref, different tenant → different pseudonym (no cross-tenant correlation)
    assert p1 != pseudonymize(RAW, T2, master_key_hex=MK)
    # shape: OUT-<24 hex>, and it's a valid pseudonym; leaks none of the original
    assert is_pseudonymized(p1) and len(p1) == 4 + 24
    assert "fasanara" not in p1 and "bianchi" not in p1
    # idempotent: pseudonymizing a pseudonym returns it unchanged (backfill re-run safe)
    assert pseudonymize(p1, T1, master_key_hex=MK) == p1
    # None passes through
    assert pseudonymize(None, T1, master_key_hex=MK) is None
    # raw refs (incl. SMOKE) are NOT mistaken for pseudonyms
    assert not is_pseudonymized(RAW) and not is_pseudonymized("OUT-SMOKE2-1")


# ── MUST-FIX: CGR still scores a pseudonymized decision ───────────────────────

def _dt():
    dt = DecisionTrailService(TEST_DB_URL); dt.ensure_schema(); return dt


def _log_decision(dt, tenant, inv_ref, enc):
    dt.log(
        tenant_id=tenant, store_id="governed",
        query=json.dumps({"tool": "send_email", "invoice_ref": inv_ref}),
        model_id="m", raw_output=json.dumps({"decision": "certify"}),
        parameters={"invoice_id": inv_ref, "invoice_ref": inv_ref,   # BOTH pseudonymized upstream
                    "decision": "certify", "agent_key": "k"*64,
                    "agent_handle": "gtm-outreach-agent@ulissy", "cgr_schema": "cgr.decision.v1"},
        encryption=enc,
    )


def test_cgr_join_and_score_survive_pseudonymization():
    T = "pseud-" + uuid.uuid4().hex[:8]
    enc = FernetEncryptor(Fernet(Fernet.generate_key()))
    dt = _dt()
    pseudo = pseudonymize(RAW, T, master_key_hex=MK)   # the per-tenant pseudonym for this ref
    assert is_pseudonymized(pseudo)

    # decision stores the PSEUDONYM in parameters.invoice_ref (as the write-path will)
    _log_decision(dt, T, pseudo, enc)

    sm = StoreManager(lambda: PostgresGMPBackend(TEST_DB_URL, encryption=enc))
    try:
        # outcome subject is the SAME pseudonym (write-path pseudonymizes both sides)
        ob = sm.get_or_create_named(CGR_OUTCOMES_STORE).backend
        meta = {"predicate": "receivable_outcome", "subject": pseudo, "object": "paid",
                "cgr_schema": CGR_OUTCOME_SCHEMA, "source": "manual"}
        ob.write(f"receivable_outcome | {pseudo} | paid", WriteOptions(tenant_id=T, metadata=meta))

        rows = load_substrate(dt, sm, T)
        mine = [r for r in rows if r.invoice_ref == pseudo]
        assert len(mine) == 1, "pseudonymized decision must be visible with its pseudonym as the ref"
        assert mine[0].outcome == "paid", \
            "MUST-FIX: pseudonymized decision joins to its pseudonymized outcome and RESOLVES"
        # and the ref carries no PII
        assert "fasanara" not in mine[0].invoice_ref and "bianchi" not in mine[0].invoice_ref
    finally:
        for e in list(getattr(sm, "_stores", {}).values()):
            try: e.backend.close()
            except Exception: pass


def test_join_breaks_if_only_one_side_pseudonymized():
    """Guard: the join is preserved ONLY when both sides use the same transform. If the decision
    is pseudonymized but the outcome subject is left raw (a backfill-consistency bug), it must NOT
    resolve — proving the test would catch an inconsistent migration."""
    T = "pseud-" + uuid.uuid4().hex[:8]
    enc = FernetEncryptor(Fernet(Fernet.generate_key()))
    dt = _dt()
    pseudo = pseudonymize(RAW, T, master_key_hex=MK)
    _log_decision(dt, T, pseudo, enc)                  # decision pseudonymized
    sm = StoreManager(lambda: PostgresGMPBackend(TEST_DB_URL, encryption=enc))
    try:
        ob = sm.get_or_create_named(CGR_OUTCOMES_STORE).backend
        ob.write(f"receivable_outcome | {RAW} | paid",  # outcome LEFT RAW (inconsistent)
                 WriteOptions(tenant_id=T, metadata={"predicate": "receivable_outcome",
                              "subject": RAW, "object": "paid", "cgr_schema": CGR_OUTCOME_SCHEMA}))
        resolved = next(r for r in load_substrate(dt, sm, T) if r.invoice_ref == pseudo)
        assert resolved.outcome is None, "mismatched transform must NOT join (catches inconsistent backfill)"
    finally:
        for e in list(getattr(sm, "_stores", {}).values()):
            try: e.backend.close()
            except Exception: pass


# ── backfill: existing decision_records get pseudonymized, idempotently ────────

def test_backfill_decisions_pseudonymizes_both_fields_idempotent():
    from ops.pseudonymize_invoice_ref import backfill_decisions
    T = "pseud-" + uuid.uuid4().hex[:8]
    dt = _dt()
    RAW2 = "OUT-akur8-thomas-holmes"
    # seed a decision holding a RAW ref (the pre-backfill state) — write directly, bypass write-path
    dt.log(tenant_id=T, store_id="governed", query="{}", model_id="m", raw_output="{}",
           parameters={"invoice_id": RAW2, "invoice_ref": RAW2, "agent_key": "k" * 64})
    with psycopg.connect(TEST_DB_URL) as conn:
        s1 = backfill_decisions(conn, T, MK)
    assert s1["updated"] == 1
    with psycopg.connect(TEST_DB_URL) as c:
        raw = c.execute("SELECT parameters FROM decision_records WHERE tenant_id=%s LIMIT 1", (T,)).fetchone()[0]
    p = raw if isinstance(raw, dict) else json.loads(raw)
    exp = pseudonymize(RAW2, T, master_key_hex=MK)
    assert p["invoice_ref"] == exp and p["invoice_id"] == exp        # BOTH fields
    assert "akur8" not in p["invoice_ref"] and "holmes" not in p["invoice_ref"]
    # idempotent: re-run is a no-op
    with psycopg.connect(TEST_DB_URL) as conn:
        s2 = backfill_decisions(conn, T, MK)
    assert s2["updated"] == 0 and s2["skipped_pseudo"] >= 1
