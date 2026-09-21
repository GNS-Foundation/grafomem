"""Runner-side backfill of tenant_api_keys.api_key_hash = HMAC_SHA256(pepper, api_key).

Importable so the migrations runner can call it IN-PROCESS as part of the pre-deploy step (no shell
chaining — Railway's preDeployCommand does not reliably run `&&` through a shell). Also used by the
standalone CLI (scripts/backfill_api_key_hash.py).

Guarantees: the pepper is HMAC input in-process ONLY (never enters SQL — the UPDATE is parameterized
on the hash bytes + key_id); hashing FAILS CLOSED on an absent/empty pepper (unless the caller opts
into a dark skip). DARK: auth still resolves by plaintext until the dual-read PR.
"""
from __future__ import annotations

import logging

import psycopg
from psycopg.rows import dict_row

from aml.server.api_key_hash import PepperMissing, compute_api_key_hash, get_pepper

logger = logging.getLogger("grafomem.migrations.apikeyhash")


def backfill(conn, pepper: str) -> dict:
    """Populate api_key_hash for every row missing it. The pepper is used only as in-process HMAC
    input; the UPDATE is parameterized on the hash bytes and key_id — never the pepper."""
    rows = conn.execute(
        "SELECT key_id, api_key FROM tenant_api_keys "
        "WHERE api_key_hash IS NULL AND api_key IS NOT NULL"
    ).fetchall()
    updated = 0
    for r in rows:
        h = compute_api_key_hash(r["api_key"], pepper)
        conn.execute(
            "UPDATE tenant_api_keys SET api_key_hash = %s WHERE key_id = %s",
            (h, r["key_id"]),
        )
        updated += 1
    return {"scanned": len(rows), "updated": updated}


def run(db_url: str, *, skip_if_no_pepper: bool = False) -> dict:
    """Backfill against `db_url` (the migrate role). FAIL CLOSED on an absent pepper unless
    `skip_if_no_pepper` (then a dark no-op). Returns a summary dict."""
    try:
        pepper = get_pepper()
    except PepperMissing:
        if skip_if_no_pepper:
            logger.info("api_key_hash backfill: skipped — GRAFOMEM_API_KEY_PEPPER not set (dark rollout)")
            return {"skipped": True, "scanned": 0, "updated": 0}
        raise
    with psycopg.connect(db_url, row_factory=dict_row, autocommit=False) as conn:
        try:
            res = backfill(conn, pepper)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    logger.info("api_key_hash backfill: scanned=%s updated=%s", res["scanned"], res["updated"])
    return res
