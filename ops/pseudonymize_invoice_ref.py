#!/usr/bin/env python3
"""One-off, idempotent backfill: pseudonymize existing invoice_ref (the last PII residual).

Replaces the plaintext `OUT-{company}-{person}` refs with their per-tenant HMAC pseudonym
(aml.cloud.invoice_pseudonym.pseudonymize) — the SAME transform the write-path now applies, so
backfilled rows match new writes and CGR's pure-equality join is preserved. Two surfaces:

  A. decision_records.parameters  — `invoice_ref` + `invoice_id` (plaintext JSONB). Bulk.
  B. CGR outcomes/reviews store   — the `subject` metadata + the content string, ENCRYPTED at
     rest (memories.metadata_enc/content_enc). Decrypt → transform → re-encrypt, in place (the
     row identity / valid_from is preserved, so append-only latest-wins semantics are intact).

Idempotent: a value already matching OUT-<24hex> is skipped (no double-HMAC). Runs INSIDE
Railway (needs GRAFOMEM_MASTER_KEY + the private DB), same pattern as encrypt_decision_context.

    railway run --service <backend> python ops/pseudonymize_invoice_ref.py --all-gtm --dry-run
    railway run --service <backend> python ops/pseudonymize_invoice_ref.py --all-gtm            # apply
"""
from __future__ import annotations

import argparse
import json
import os
import sys

GTM_TENANTS = [
    "5605470cfa8e415ba418c9d8944abf9a",   # corp
    "600e0890aa9042acaabe4b1c3d4fbdc5",   # machine
    "e1c5e0619cdd42c38f59b5079e9d18e4",   # orphan (the ONLY one with outcome/review copies)
]


def _pseudo(ref, tenant_id, master_key_hex):
    from aml.cloud.invoice_pseudonym import pseudonymize
    return pseudonymize(ref, tenant_id, master_key_hex=master_key_hex)


def _is_pseudo(ref):
    from aml.cloud.invoice_pseudonym import is_pseudonymized
    return is_pseudonymized(ref)


# Step-0 ground-truth row counts per GTM tenant — the guard asserts the scan matches these, so
# an RLS fail-closed (0-row) scan under grafomem_rt can't pass as a silent no-op.
EXPECTED_DECISIONS = {
    "5605470cfa8e415ba418c9d8944abf9a": 34,
    "600e0890aa9042acaabe4b1c3d4fbdc5": 21,
    "e1c5e0619cdd42c38f59b5079e9d18e4": 6,
}
EXPECTED_MEMORIES = {"e1c5e0619cdd42c38f59b5079e9d18e4": 7}


def _set_tenant_ctx(conn, tenant_id):
    """RLS: the backfill runs AS grafomem_rt (post-flip GRAFOMEM_DB_URL), so without the tenant
    context every policied SELECT/UPDATE fail-closes to 0 rows. Set it before touching data."""
    conn.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant_id,))


# ── A. decision_records.parameters (plaintext JSONB) ──────────────────────────
def backfill_decisions(conn, tenant_id, master_key_hex, *, dry_run=False):
    _set_tenant_ctx(conn, tenant_id)
    rows = conn.execute(
        "SELECT decision_id, parameters FROM decision_records "
        "WHERE tenant_id = %s AND parameters ? 'invoice_ref'", (tenant_id,),
    ).fetchall()
    _exp = EXPECTED_DECISIONS.get(tenant_id)
    if _exp is not None and len(rows) < _exp:            # floor: growth ok, a shortfall (esp. 0) is not
        raise RuntimeError(f"SCAN GUARD (decisions): tenant {tenant_id} scanned={len(rows)} < Step-0 {_exp}. "
                           f"Likely RLS fail-closed (no tenant context, running as grafomem_rt) — ABORT before any write.")
    stats = {"scanned": len(rows), "updated": 0, "skipped_pseudo": 0}
    for did, params in rows:
        p = params if isinstance(params, dict) else json.loads(params)
        ref = p.get("invoice_ref")
        if ref is None or _is_pseudo(ref):
            stats["skipped_pseudo"] += 1
            continue
        new = _pseudo(ref, tenant_id, master_key_hex)
        p["invoice_ref"] = new
        if p.get("invoice_id") is not None:            # invoice_id == invoice_ref (Step-0 confirmed)
            p["invoice_id"] = _pseudo(p["invoice_id"], tenant_id, master_key_hex)
        if not dry_run:
            conn.execute("UPDATE decision_records SET parameters = %s::jsonb WHERE decision_id = %s",
                         (json.dumps(p), did))
        stats["updated"] += 1
    if not dry_run:
        conn.commit()
    return stats


# ── B. CGR outcomes/reviews store (encrypted subject + content in `memories`) ──
def backfill_cgr_memories(conn, encryptor, tenant_id, master_key_hex, *, dry_run=False):
    _set_tenant_ctx(conn, tenant_id)                    # memories is FORCE RLS — context required
    rows = conn.execute(
        "SELECT ref, content_enc, metadata_enc FROM memories "
        "WHERE tenant_id = %s AND metadata_enc IS NOT NULL", (tenant_id,),
    ).fetchall()
    _exp = EXPECTED_MEMORIES.get(tenant_id)              # only e1c5e06 has outcome/review copies
    if _exp is not None and len(rows) < _exp:
        raise RuntimeError(f"SCAN GUARD (memories): tenant {tenant_id} scanned={len(rows)} < Step-0 {_exp}. "
                           f"Likely RLS fail-closed (memories is FORCE RLS) — ABORT before any write.")
    stats = {"scanned": len(rows), "updated": 0, "skipped": 0}
    for ref, content_enc, metadata_enc in rows:
        meta = json.loads(encryptor.decrypt(metadata_enc))
        subj = meta.get("subject")
        # only the outcome/review rows carry an invoice_ref as `subject`
        if not subj or not str(subj).startswith("OUT-") or _is_pseudo(subj):
            stats["skipped"] += 1
            continue
        new = _pseudo(subj, tenant_id, master_key_hex)
        meta["subject"] = new
        content = encryptor.decrypt(content_enc) if content_enc else None
        new_content_enc = encryptor.encrypt(content.replace(subj, new)) if content else content_enc
        new_metadata_enc = encryptor.encrypt(json.dumps(meta))
        if not dry_run:
            conn.execute("UPDATE memories SET metadata_enc = %s, content_enc = %s WHERE ref = %s AND tenant_id = %s",
                         (new_metadata_enc, new_content_enc, ref, tenant_id))
        stats["updated"] += 1
    if not dry_run:
        conn.commit()
    return stats


def _build_conn_and_encryptor(tenant_id):
    import psycopg
    from aml.cloud.identity import EnvIdentity
    db_url = os.environ.get("GRAFOMEM_DB_URL") or sys.exit("GRAFOMEM_DB_URL not set")
    master = os.environ.get("GRAFOMEM_MASTER_KEY") or sys.exit("GRAFOMEM_MASTER_KEY not set (pseudonym HMAC key)")
    if not os.environ.get("PROVIDER_ENCRYPTION_KEY"):
        sys.exit("PROVIDER_ENCRYPTION_KEY not set (the CGR memories store's encryptor)")
    conn = psycopg.connect(db_url)
    # NB: the CGR outcome/review store (memories) is encrypted with EnvIdentity (PROVIDER_ENCRYPTION_KEY,
    # tenant-agnostic) — NOT the per-tenant DEK that decision_records.query_enc uses. Verified empirically.
    enc = EnvIdentity()
    return conn, enc, master


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tenant", default=GTM_TENANTS[0], help="tenant_id (comma-separated ok)")
    ap.add_argument("--all-gtm", action="store_true", help="all 3 GTM tenants")
    ap.add_argument("--dry-run", action="store_true", help="report counts, no writes")
    args = ap.parse_args(argv)
    tenants = GTM_TENANTS if args.all_gtm else [t.strip() for t in args.tenant.split(",") if t.strip()]

    for t in tenants:
        conn, enc, master = _build_conn_and_encryptor(t)
        try:
            d = backfill_decisions(conn, t, master, dry_run=args.dry_run)
            m = backfill_cgr_memories(conn, enc, t, master, dry_run=args.dry_run)
        finally:
            conn.close()
        mode = "DRY-RUN" if args.dry_run else "APPLIED"
        print(f"[{mode}] tenant={t}")
        print(f"  decisions: scanned={d['scanned']} updated={d['updated']} skipped_pseudo={d['skipped_pseudo']}")
        print(f"  cgr-memories: scanned={m['scanned']} updated={m['updated']} skipped={m['skipped']}")


if __name__ == "__main__":
    main()
