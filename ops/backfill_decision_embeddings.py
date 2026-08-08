#!/usr/bin/env python3
"""Manifold Phase-0.5 backfill — embed existing governed decisions into the decision_embeddings vault.

For each CGR-attributed decision_record: decrypt query_enc + raw_output_enc (per-tenant DEK),
compose the SAME capability text as the write-path hook (DecisionTrailService.capability_text),
redact, embed (BAAI/bge-small-en-v1.5, 384-d), and INSERT the vector ONLY — never the plaintext.

RLS-aware + idempotent + scan-guarded (the invoice_ref lesson): sets app.current_tenant per tenant
(so a grafomem_rt run doesn't fail-close to 0 rows), skips decision_ids already embedded
(ON CONFLICT DO NOTHING + a pre-scan), and ABORTS if a per-tenant scan comes back below its Step-0
floor (a shortfall = an RLS/context miss, not an empty tenant).

Runs INSIDE the vault (needs GRAFOMEM_MASTER_KEY for the per-tenant DEK + the embedder). No scoring
change. Apply AFTER ops/decision_embeddings.sql is live.

    railway run --service <backend> python ops/backfill_decision_embeddings.py --all-gtm --dry-run
    railway run --service <backend> python ops/backfill_decision_embeddings.py --all-gtm            # apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys

GTM_TENANTS = [
    "5605470cfa8e415ba418c9d8944abf9a",   # corp
    "600e0890aa9042acaabe4b1c3d4fbdc5",   # machine
    "e1c5e0619cdd42c38f59b5079e9d18e4",   # orphan
]

# Step-0 ground-truth floors (CGR-attributed decisions per tenant, from the substrate export). A
# per-tenant scan below this ⇒ likely RLS fail-closed under grafomem_rt ⇒ ABORT before any write.
EXPECTED_DECISIONS = {
    "5605470cfa8e415ba418c9d8944abf9a": 43,
    "600e0890aa9042acaabe4b1c3d4fbdc5": 21,
    "e1c5e0619cdd42c38f59b5079e9d18e4": 6,
    "1e5d30a0f72d4da7aaad7dd0d68d36e9": 3,   # devtest-track2 (eng-agent)
}


def _set_tenant_ctx(conn, tenant_id):
    conn.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant_id,))


def backfill(conn, tenant_id, tkm, embed_fn, *, dry_run=False):
    import numpy as np
    from aml.cloud.decision_trail import DecisionTrailService, _redact_pii

    _set_tenant_ctx(conn, tenant_id)
    rows = conn.execute(
        "SELECT decision_id, query_enc, raw_output_enc, query, raw_output, parameters "
        "FROM decision_records WHERE tenant_id = %s AND parameters ? 'cgr_schema'", (tenant_id,),
    ).fetchall()
    floor = EXPECTED_DECISIONS.get(tenant_id)
    if floor is not None and len(rows) < floor:
        raise RuntimeError(f"SCAN GUARD: tenant {tenant_id} scanned={len(rows)} < Step-0 {floor}. "
                           f"Likely RLS fail-closed (no tenant context as grafomem_rt) — ABORT before any write.")

    done = {r[0] for r in conn.execute(
        "SELECT decision_id FROM decision_embeddings WHERE tenant_id = %s", (tenant_id,)).fetchall()}
    enc = tkm.get_encryptor(tenant_id) if tkm else None
    stats = {"scanned": len(rows), "embedded": 0, "skipped_existing": 0, "decrypt_fail": 0}

    def _plain(enc_val, plain_val):
        """Decrypt the encrypted variant when present (prod); else use the plaintext column
        (unencrypted dev/test rows store content in `query`/`raw_output`, not the *_enc columns)."""
        if enc and enc_val:
            return enc.decrypt(enc_val)
        return "" if plain_val in (None, "[ENCRYPTED]") else plain_val

    for did, qenc, renc, qplain, rplain, params in rows:
        if did in done:
            stats["skipped_existing"] += 1
            continue
        try:
            query = _plain(qenc, qplain)
            raw = _plain(renc, rplain)
        except Exception:
            stats["decrypt_fail"] += 1
            continue
        p = params if isinstance(params, dict) else json.loads(params)
        text = _redact_pii(DecisionTrailService.capability_text(query, raw, p))
        vec = np.asarray(embed_fn([text]), dtype=float).reshape(-1)
        lit = "[" + ",".join(f"{float(x):.8g}" for x in vec) + "]"
        if not dry_run:
            conn.execute(
                "INSERT INTO decision_embeddings "
                "(tenant_id, decision_id, embedding, tokenizer_id, created_at, valid_from) "
                "VALUES (%s, %s, %s::vector, %s, now(), now()) ON CONFLICT (tenant_id, decision_id) DO NOTHING",
                (tenant_id, did, lit, "BAAI/bge-small-en-v1.5"),
            )
        stats["embedded"] += 1
    if not dry_run:
        conn.commit()
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tenant", default=GTM_TENANTS[0], help="tenant_id (comma-separated ok)")
    ap.add_argument("--all-gtm", action="store_true", help="the 3 GTM tenants")
    ap.add_argument("--dry-run", action="store_true", help="scan + guard + count, no writes/embeds")
    args = ap.parse_args(argv)
    tenants = GTM_TENANTS if args.all_gtm else [t.strip() for t in args.tenant.split(",") if t.strip()]

    import psycopg
    from aml.cloud.tenant_key_manager import TenantKeyManager
    db_url = os.environ.get("GRAFOMEM_DB_URL") or sys.exit("GRAFOMEM_DB_URL not set")
    master = os.environ.get("GRAFOMEM_MASTER_KEY") or sys.exit("GRAFOMEM_MASTER_KEY not set (per-tenant DEK)")
    tkm = TenantKeyManager(master, db_url)
    embed_fn = None
    if not args.dry_run:
        from aml.backends.vector_only import _default_embedder
        embed_fn = _default_embedder()
    else:
        embed_fn = (lambda texts: [[0.0]])   # unused in dry-run

    for t in tenants:
        conn = psycopg.connect(db_url)
        try:
            s = backfill(conn, t, tkm, embed_fn, dry_run=args.dry_run)
        finally:
            conn.close()
        mode = "DRY-RUN" if args.dry_run else "APPLIED"
        print(f"[{mode}] tenant={t} scanned={s['scanned']} embedded={s['embedded']} "
              f"skipped_existing={s['skipped_existing']} decrypt_fail={s['decrypt_fail']}")


if __name__ == "__main__":
    main()
