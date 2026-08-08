"""Manifold Phase-0.5 — decision-content embedding (vault-only) tests.

Covers the Cowork-mandated properties: the write-path hook (fail-open) mints a vector into the
decision_embeddings VAULT and NO plaintext; the composed capability text excludes the pseudonymized
invoice_ref + agent identity; paid-like vs default-like SYNTHETIC content separates (> +0.06, the
plumbing gate — not a claim on the real n=5); the table is never serialized by any API (vault-only,
by source scan); the backfill is idempotent + scan-guarded. RLS fail-closed coverage lives in
tests/test_rls_decision_hitl.py (decision_embeddings is in its REAL_TABLES).

Uses the fast deterministic _stub_embedder(384) (disjoint vocab ⇒ embedder-agnostic separation) so
the gate tests the pipeline, not the heavy BGE model.
"""
from __future__ import annotations

import json
import uuid

import numpy as np
import psycopg
import pytest

from aml.backends.vector_only import _stub_embedder
from aml.cloud.decision_trail import DecisionTrailService

TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"


def _dt(embed_fn=None):
    dt = DecisionTrailService(TEST_DB_URL, embed_fn=embed_fn, tokenizer_id="stub-384")
    dt.ensure_schema()
    return dt


def _log(dt, tenant, *, query, raw_output, tool="deploy", inv="OUT-" + "a" * 24):
    return dt.log(
        tenant_id=tenant, store_id="governed", query=query, model_id="m", raw_output=raw_output,
        parameters={"invoice_ref": inv, "invoice_id": inv, "decision": "certify",
                    "verifiability_tag": "judgment", "cgr_schema": "cgr.decision.v1",
                    "agent_key": "k" * 64, "agent_handle": "eng-agent@ulissy", "tool": tool},
    )


def _vec(tenant, decision_id):
    with psycopg.connect(TEST_DB_URL, autocommit=True) as c:
        row = c.execute("SELECT embedding::text FROM decision_embeddings WHERE tenant_id=%s AND decision_id=%s",
                        (tenant, decision_id)).fetchone()
    return np.asarray([float(x) for x in row[0].strip("[]").split(",")]) if row else None


# ── capability text: excludes the pseudonym + identity, keeps capability signal ──

def test_capability_text_excludes_ref_and_identity():
    inv = "OUT-587495cad195f29f8b5ac7aa"
    params = {"invoice_ref": inv, "invoice_id": inv, "agent_key": "k" * 64,
              "agent_handle": "gtm-outreach-agent@ulissy", "decision": "certify",
              "verifiability_tag": "judgment", "cgr_schema": "cgr.decision.v1", "tool": "send_email"}
    query = json.dumps({"tool": "send_email", "invoice_ref": inv, "args": {"segment": "prime borrower"}})
    raw = json.dumps({"decision": "certify", "reason": "strong repayment history"})
    text = DecisionTrailService.capability_text(query, raw, params)
    # capability signal present
    assert "certify" in text and "judgment" in text and "send_email" in text
    assert "prime borrower" in text and "strong repayment history" in text
    # excluded: the pseudonymized ref token + the agent identity
    assert inv not in text, "pseudonymized invoice_ref must never be embedded"
    assert "k" * 64 not in text and "gtm-outreach-agent@ulissy" not in text


# ── write-path hook: mints a vector, persists NO plaintext, fail-open ──

def test_hook_writes_vector_only():
    dt = _dt(embed_fn=_stub_embedder(384))
    T = "de-" + uuid.uuid4().hex[:8]
    marker = "ZZUNIQUEPII" + uuid.uuid4().hex     # a unique token planted in the (redacted) content
    rec = _log(dt, T, query=json.dumps({"tool": "deploy", "args": {"note": marker}}),
               raw_output=json.dumps({"decision": "certify", "reason": "green ci"}))
    v = _vec(T, rec.decision_id)
    assert v is not None and v.shape[0] == 384, "hook must store a 384-d vector"
    # NO plaintext leak: the whole row rendered as text contains no source content, only numbers/ids
    with psycopg.connect(TEST_DB_URL, autocommit=True) as c:
        cols = [r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='decision_embeddings'").fetchall()]
        row_txt = str(c.execute("SELECT * FROM decision_embeddings WHERE tenant_id=%s", (T,)).fetchone())
    assert not ({"content", "query", "text", "raw_output", "metadata"} & set(cols)), \
        f"decision_embeddings must carry NO plaintext content column; has {cols}"
    assert marker not in row_txt, "planted content token must not appear in the stored row"


def test_hook_is_fail_open_and_never_blocks_the_decision():
    def _boom(_texts):
        raise RuntimeError("embedder down")
    dt = _dt(embed_fn=_boom)
    T = "de-" + uuid.uuid4().hex[:8]
    rec = _log(dt, T, query="{}", raw_output="{}")          # must NOT raise
    # the governed decision persisted…
    with psycopg.connect(TEST_DB_URL, autocommit=True) as c:
        got = c.execute("SELECT count(*) FROM decision_records WHERE decision_id=%s", (rec.decision_id,)).fetchone()[0]
    assert got == 1
    # …and no vector was written (fail-open dropped it)
    assert _vec(T, rec.decision_id) is None


def test_hook_noop_without_embedder():
    dt = _dt(embed_fn=None)                                  # no embedder ⇒ hook disabled
    T = "de-" + uuid.uuid4().hex[:8]
    rec = _log(dt, T, query="{}", raw_output="{}")
    assert _vec(T, rec.decision_id) is None


def test_hook_skips_non_cgr_decisions():
    dt = _dt(embed_fn=_stub_embedder(384))
    T = "de-" + uuid.uuid4().hex[:8]
    rec = dt.log(tenant_id=T, store_id="governed", query="{}", model_id="m", raw_output="{}",
                 parameters={"decision": "certify"})        # no cgr_schema ⇒ not embedded
    assert _vec(T, rec.decision_id) is None


# ── acceptance metric (synthetic): paid-like vs default-like content separates > +0.06 ──

def _separation(paid, default):
    def cos(a, b):
        return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))
    labelled = [("p", v) for v in paid] + [("d", v) for v in default]
    within, across = [], []
    for i in range(len(labelled)):
        for j in range(i + 1, len(labelled)):
            (li, vi), (lj, vj) = labelled[i], labelled[j]
            (within if li == lj else across).append(cos(vi, vj))
    return sum(within) / len(within) - sum(across) / len(across)


def test_acceptance_paid_default_separation_beats_threshold():
    dt = _dt(embed_fn=_stub_embedder(384))
    T = "de-" + uuid.uuid4().hex[:8]
    # disjoint capability vocabularies for the two outcome classes (embedder-agnostic separation)
    paid_txt = ["reliable creditworthy prompt stable solvent audited verified compliant"]
    def_txt = ["delinquent bankrupt disputed overdue fraudulent insolvent charged-off default"]
    paid_ids, def_ids = [], []
    for k in range(3):
        r = _log(dt, T, query=json.dumps({"tool": "deploy", "args": {"profile": paid_txt[0] + f" run{k}"}}),
                 raw_output=json.dumps({"decision": "certify", "reason": paid_txt[0]}))
        paid_ids.append(r.decision_id)
        r = _log(dt, T, query=json.dumps({"tool": "deploy", "args": {"profile": def_txt[0] + f" run{k}"}}),
                 raw_output=json.dumps({"decision": "certify", "reason": def_txt[0]}))
        def_ids.append(r.decision_id)
    paid = [_vec(T, i) for i in paid_ids]
    default = [_vec(T, i) for i in def_ids]
    assert all(v is not None for v in paid + default)
    sep = _separation(paid, default)
    assert sep > 0.06, f"paid/default content must separate > +0.06 (plumbing gate); got {sep:.4f}"


# ── vault-only: no API path / serializer returns decision vectors ──

def test_vault_only_no_api_returns_decision_embeddings():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "aml"
    # modules allowed to touch the vault table (write hook + erasure); NOT routes/exporters
    allowed = {"decision_trail.py", "erasure_sweeper.py"}
    offenders = []
    for p in root.rglob("*.py"):
        if "decision_embeddings" in p.read_text():
            is_route_or_export = ("routes" in p.name) or (p.name in {"manifold.py", "demo_routes.py"})
            if p.name not in allowed and is_route_or_export:
                offenders.append(str(p.relative_to(root)))
    assert not offenders, f"decision_embeddings must not be referenced by any route/exporter: {offenders}"


# ── backfill: idempotent + scan-guarded ──

def test_backfill_idempotent_and_scan_guarded():
    from ops import backfill_decision_embeddings as bf
    T = "de-" + uuid.uuid4().hex[:8]
    dt = _dt(embed_fn=None)                                  # seed decisions WITHOUT the hook
    for k in range(3):
        _log(dt, T, query=json.dumps({"tool": "deploy", "args": {"n": k}}),
             raw_output=json.dumps({"decision": "certify", "reason": f"r{k}"}))
    embed = _stub_embedder(384)
    with psycopg.connect(TEST_DB_URL) as conn:
        s1 = bf.backfill(conn, T, None, embed, dry_run=False)      # tkm=None ⇒ query_enc is plaintext here
    assert s1["scanned"] == 3 and s1["embedded"] == 3
    with psycopg.connect(TEST_DB_URL) as conn:
        s2 = bf.backfill(conn, T, None, embed, dry_run=False)      # idempotent: nothing new
    assert s2["embedded"] == 0 and s2["skipped_existing"] == 3
    # scan-guard: an inflated floor (simulating an RLS 0-row fail-closed) aborts before any write
    bf.EXPECTED_DECISIONS[T] = 99
    try:
        with psycopg.connect(TEST_DB_URL) as conn:
            with pytest.raises(RuntimeError, match="SCAN GUARD"):
                bf.backfill(conn, T, None, embed, dry_run=False)
    finally:
        bf.EXPECTED_DECISIONS.pop(T, None)
