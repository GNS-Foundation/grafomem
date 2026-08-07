#!/usr/bin/env python3
"""One-off, idempotent re-encryption of plaintext decision-record context (PII) at rest.

Mauricio gate B. Backfills `decision_records` rows whose context (`query`) — and the
paired `raw_output` — were written in PLAINTEXT before `propose_action` passed an
encryptor (the class fix in this same PR). Encrypts them in place to the SAME shape the
write path produces under encryption: the plaintext column becomes the "[ENCRYPTED]"
sentinel and the ciphertext lands in the paired `_enc` column.

WHY a Python script (not SQL): the ciphertext is Fernet over the tenant's DEK; only the
running app env has `GRAFOMEM_MASTER_KEY` + the `tenant_deks` store. It MUST run inside
the Railway network (private `postgres.railway.internal`) with the prod env, e.g.:

    railway run --service <backend> python ops/encrypt_decision_context.py --dry-run
    railway run --service <backend> python ops/encrypt_decision_context.py            # apply
    railway run --service <backend> python ops/encrypt_decision_context.py --verify

It reuses the SAME per-tenant DEK the write path uses (TenantKeyManager.get_encryptor),
so migrated rows decrypt uniformly with natively-encrypted ones. NOT wired into startup.

Idempotency: per-column, gated on the "[ENCRYPTED]" sentinel — re-running is a no-op and
never double-encrypts. CGR is unaffected: it reads `parameters` (JSONB, never encrypted),
never `query`/`raw_output`.
"""
from __future__ import annotations

import argparse
import os
import sys

# Tenants carrying gtm-outreach-agent@ulissy plaintext PII (verified live 2026-08-07):
#   corp keeper 5605470c (34 rows) + phase-0 machine tenants 600e0890 (21) / e1c5e06 (6).
# All 61 rows are plaintext prospect company names; there is no tenant-purge path, so the
# machine-tenant rows persist unless encrypted here. Default targets corp only; pass
# --all-gtm (or a comma list to --tenant) to cover all three.
CORP_TENANT_ID = "5605470cfa8e415ba418c9d8944abf9a"
GTM_TENANTS = [
    "5605470cfa8e415ba418c9d8944abf9a",   # corp (cayerbe@ulissy.app) — keeper
    "600e0890aa9042acaabe4b1c3d4fbdc5",   # phase-0 machine tenant (the brief's "~21")
    "e1c5e0619cdd42c38f59b5079e9d18e4",   # phase-0 orphaned throwaway
]

_SENTINEL = "[ENCRYPTED]"
# The two content-bearing columns the write path encrypts and that these rows populate.
# (retrieved_contents/parsed_output are empty/null for propose_action rows and their
#  canonical representation is identical encrypted-or-not, so they need no backfill.)
_COLS = [("query", "query_enc"), ("raw_output", "raw_output_enc")]


def _looks_like_pii(sample_rows) -> bool:
    return any(r.get("query") not in (None, _SENTINEL) for r in sample_rows)


def reencrypt_decision_context(conn, encryptor, tenant_id, *, dry_run=False):
    """Encrypt any plaintext `query`/`raw_output` for `tenant_id` in decision_records.

    `conn`      : a live psycopg connection (dict_row not required; we pass explicit cols).
    `encryptor` : object with .encrypt(str)->str / .decrypt(str)->str (per-tenant Fernet).
    Returns a stats dict. Commits on apply (pooled-connection safe — explicit commit)."""
    # Select rows where AT LEAST ONE content column is still plaintext.
    rows = conn.execute(
        "SELECT decision_id, query, raw_output, query_enc, raw_output_enc "
        "FROM decision_records "
        "WHERE tenant_id = %s AND (query <> %s OR raw_output <> %s) "
        "ORDER BY created_at",
        (tenant_id, _SENTINEL, _SENTINEL),
    ).fetchall()

    # psycopg default row is a tuple; normalize to dict by position.
    def _as_dict(r):
        if isinstance(r, dict):
            return r
        return {"decision_id": r[0], "query": r[1], "raw_output": r[2],
                "query_enc": r[3], "raw_output_enc": r[4]}

    scanned = [_as_dict(r) for r in rows]
    stats = {"tenant_id": tenant_id, "scanned": len(scanned),
             "encrypted_rows": 0, "columns_written": 0, "dry_run": dry_run,
             "sample_plaintext": []}

    for r in scanned[:3]:
        # Redacted sample for the operator: prove it's plaintext without dumping full PII.
        q = r.get("query") or ""
        stats["sample_plaintext"].append({
            "decision_id": r["decision_id"],
            "query_prefix": (q[:48] + "…") if q and q != _SENTINEL else q,
            "query_is_plaintext": q not in (None, _SENTINEL),
        })

    if dry_run:
        return stats

    for r in scanned:
        sets, params = [], []
        for plain_col, enc_col in _COLS:
            val = r.get(plain_col)
            if val is None or val == _SENTINEL:
                continue                              # already encrypted / nothing to do
            sets.append(f"{plain_col} = %s")
            params.append(_SENTINEL)
            sets.append(f"{enc_col} = %s")
            params.append(encryptor.encrypt(val))
            stats["columns_written"] += 1
        if not sets:
            continue
        params.append(r["decision_id"])
        conn.execute(
            f"UPDATE decision_records SET {', '.join(sets)} WHERE decision_id = %s",
            params,
        )
        stats["encrypted_rows"] += 1

    conn.commit()                                    # explicit — do not rely on autocommit
    return stats


def count_plaintext(conn, tenant_id) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM decision_records WHERE tenant_id = %s AND query <> %s",
        (tenant_id, _SENTINEL),
    ).fetchone()
    return int(row[0] if not isinstance(row, dict) else row["count"])


def count_plaintext_llm_providers(conn, tenant_id=None) -> int:
    """Rows whose api_key is present but NOT a Fernet token (gAAAAA…) ⇒ stored plaintext.
    Scoped to a tenant if given, else global. Heuristic — Fernet tokens are the only
    ciphertext shape the provider write path produces."""
    where = "api_key IS NOT NULL AND api_key NOT LIKE 'gAAAAA%'"
    params: list = []
    if tenant_id:
        where += " AND tenant_id = %s"
        params.append(tenant_id)
    row = conn.execute(
        f"SELECT COUNT(*) FROM llm_providers WHERE {where}", params
    ).fetchone()
    return int(row[0] if not isinstance(row, dict) else row["count"])


def _build_conn_and_encryptor(tenant_id):
    import psycopg
    from aml.cloud.tenant_key_manager import TenantKeyManager

    db_url = os.environ.get("GRAFOMEM_DB_URL")
    master = os.environ.get("GRAFOMEM_MASTER_KEY")
    if not db_url:
        sys.exit("GRAFOMEM_DB_URL not set — run inside the Railway backend service env.")
    if not master:
        sys.exit("GRAFOMEM_MASTER_KEY not set — run inside the Railway backend service env.")
    conn = psycopg.connect(db_url)
    tkm = TenantKeyManager(master, db_url)
    encryptor = tkm.get_encryptor(tenant_id)         # SAME DEK the write path uses
    return conn, encryptor


def _resolve_tenants(args) -> list[str]:
    if args.all_gtm:
        return list(GTM_TENANTS)
    # --tenant accepts a single id or a comma-separated list.
    return [t.strip() for t in args.tenant.split(",") if t.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tenant", default=CORP_TENANT_ID,
                    help="tenant_id to backfill (comma-separated for multiple)")
    ap.add_argument("--all-gtm", action="store_true",
                    help=f"target all {len(GTM_TENANTS)} gtm-outreach tenants (corp + 2 machine)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the plaintext-context count + a redacted sample; NO writes")
    ap.add_argument("--verify", action="store_true",
                    help="assert 0 plaintext decision_records rows; also report plaintext llm_providers")
    args = ap.parse_args(argv)
    tenants = _resolve_tenants(args)

    if args.verify:
        import psycopg
        db_url = os.environ.get("GRAFOMEM_DB_URL") or sys.exit("GRAFOMEM_DB_URL not set")
        ok = True
        with psycopg.connect(db_url) as conn:
            for t in tenants:
                dr = count_plaintext(conn, t)
                lp = count_plaintext_llm_providers(conn, t)
                print(f"[verify] tenant={t}")
                print(f"[verify]   plaintext decision_records (query<>'{_SENTINEL}'): {dr}   (want 0)")
                print(f"[verify]   plaintext llm_providers (this tenant): {lp}   (want 0)")
                ok = ok and dr == 0 and lp == 0
            print(f"[verify] plaintext llm_providers (ALL tenants): {count_plaintext_llm_providers(conn, None)}")
        sys.exit(0 if ok else 1)

    all_stats = []
    for t in tenants:
        conn, encryptor = _build_conn_and_encryptor(t)     # per-tenant DEK
        try:
            stats = reencrypt_decision_context(conn, encryptor, t, dry_run=args.dry_run)
        finally:
            conn.close()
        all_stats.append(stats)

        mode = "DRY-RUN (no writes)" if args.dry_run else "APPLIED"
        print(f"[{mode}] tenant={stats['tenant_id']}")
        print(f"  scanned (>=1 plaintext content col): {stats['scanned']}")
        if args.dry_run:
            print(f"  ⇒ would encrypt these rows. Sample (redacted):")
            for s in stats["sample_plaintext"]:
                print(f"    - {s['decision_id']}  plaintext={s['query_is_plaintext']}  q='{s['query_prefix']}'")
        else:
            print(f"  rows encrypted: {stats['encrypted_rows']}   columns written: {stats['columns_written']}")

    if len(all_stats) > 1:
        tot_scanned = sum(s["scanned"] for s in all_stats)
        tot_enc = sum(s["encrypted_rows"] for s in all_stats)
        print(f"[TOTAL over {len(all_stats)} tenants] scanned={tot_scanned} encrypted={tot_enc}")
    return all_stats


if __name__ == "__main__":
    main()
