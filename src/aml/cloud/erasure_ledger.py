"""Append-only, hash-chained erasure ledger (decisions 0013, 0017).

Records tenant-level crypto-erasures and subject-level Article 17 erasures. It is the
authoritative log used to re-scrub revived data after a backup restore (0013), and —
since 0017 — a hash chain, so that a row dropped or reordered by a higher privilege, or
by restoring an older backup of the ledger itself, is detectable.

Chain construction is byte-identical to the LangGraph adapter's ``receipt_log.py`` so one
verifier reads both logs::

    entry_hash = sha256(prev_hash || canonical(payload))
    canonical  = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    GENESIS    = "0" * 64

``payload`` is the row itself (see :func:`chain_payload`). Values must be JSONB-stable —
str / int / bool / None / nested dicts and lists of those — because the verifier rebuilds
the payload from the row as Postgres returns it. ``created_at`` is set by the writer (not
the DB default) and canonicalised to UTC ISO-8601 with microseconds, so it hashes the same
on the way in and the way out regardless of the session time zone.

Rows that predate migration 021 form a *legacy segment*: they carry a backfilled ``seq``
and NULL hashes. One sealing row (``entry_type = "chain_genesis"``) commits to their
ordered ``entry_id``s; every row after it is chained. :func:`verify_ledger` recomputes both.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

GENESIS_HASH = "0" * 64
GENESIS_ENTRY_ID = "chain_genesis"
GENESIS_ENTRY_TYPE = "chain_genesis"
GENESIS_TENANT_ID = "__ledger__"

# Columns that make up the hashed payload of a chained row, in no particular order
# (canonicalisation sorts keys). `seq`, `prev_hash`, `entry_hash` are chain metadata and
# are NOT part of the payload (prev_hash is the chain input; seq is checked separately).
_PAYLOAD_COLUMNS = (
    "entry_id", "tenant_id", "entry_type", "fact_ref", "content_hash",
    "certificate", "backfill", "created_at",
)

_CHAIN_LOCK_KEY = "erasure_ledger_chain"  # pg_advisory_xact_lock(hashtext(...))


# ---------------------------------------------------------------------------
# Pure functions (no DB) — shared by the writer and the verifier
# ---------------------------------------------------------------------------

def canonical(obj: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace. Same as the adapter's receipt_log."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _utc_iso(ts: Any) -> Any:
    """Canonicalise a timestamp to UTC ISO-8601 with microseconds; pass strings through."""
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return ts


def chain_payload(row: dict) -> dict:
    """The hashed payload of a ledger row: the row's content columns, canonicalised."""
    payload = {}
    for col in _PAYLOAD_COLUMNS:
        v = row.get(col)
        if col == "created_at":
            v = _utc_iso(v)
        payload[col] = v
    return payload


def entry_hash(prev_hash: str, payload: dict) -> str:
    return hashlib.sha256((prev_hash + canonical(payload)).encode("utf-8")).hexdigest()


def genesis_hash(legacy_entry_ids: list[str]) -> str:
    """The sealing row's entry_hash: commits to the ordered legacy entry_ids.

    Must reproduce migration 021's SQL byte for byte:
    sha256(GENESIS || '{"entry_type":"chain_genesis","legacy_entry_ids":[...]}').
    """
    payload = {"entry_type": GENESIS_ENTRY_TYPE, "legacy_entry_ids": list(legacy_entry_ids)}
    return entry_hash(GENESIS_HASH, payload)


def verify_ledger(rows: Iterable[dict]) -> tuple[bool, list[str]]:
    """Verify a ledger read back in full (all tenants, including the genesis row).

    Checks: (1) ``seq`` is present, unique and contiguous from 1; (2) exactly one
    ``chain_genesis`` row; (3) its ``entry_hash`` recomputes from the ordered legacy
    entry_ids before it (so a legacy row dropped or reordered is detected); (4) every
    row after it links to the previous row's ``entry_hash`` and its own ``entry_hash``
    recomputes from its payload. Returns ``(ok, problems)``; ``problems`` names every
    failure with its ``seq`` so an auditor can localise the break. An empty ledger
    (not yet sealed) is reported as a problem, not as trivially valid.
    """
    rows = sorted(list(rows), key=lambda r: (r.get("seq") is None, r.get("seq") or 0))
    problems: list[str] = []
    if not rows:
        return False, ["ledger is empty or unsealed: no chain_genesis row"]

    # (1) seq integrity
    seqs = [r.get("seq") for r in rows]
    if any(s is None for s in seqs):
        problems.append("rows without seq: ledger not migrated (021)")
        return False, problems
    if seqs != list(range(1, len(rows) + 1)):
        problems.append(f"seq not contiguous from 1: got {seqs[:5]}{'...' if len(seqs) > 5 else ''}")

    # (2) exactly one genesis row
    gen_idx = [i for i, r in enumerate(rows) if r.get("entry_type") == GENESIS_ENTRY_TYPE]
    if len(gen_idx) != 1:
        problems.append(f"expected exactly one chain_genesis row, found {len(gen_idx)}")
        return False, problems
    g = gen_idx[0]
    genesis = rows[g]

    # (3) legacy segment sealed by the genesis row
    legacy_ids = [r["entry_id"] for r in rows[:g]]
    expected_genesis = genesis_hash(legacy_ids)
    if genesis.get("prev_hash") != GENESIS_HASH:
        problems.append(f"seq {genesis['seq']}: genesis prev_hash is not GENESIS")
    if genesis.get("entry_hash") != expected_genesis:
        problems.append(
            f"seq {genesis['seq']}: genesis hash mismatch — the legacy segment "
            f"({len(legacy_ids)} rows) was altered, reordered or had a row removed"
        )
    for r in rows[:g]:
        if r.get("entry_hash") is not None or r.get("prev_hash") is not None:
            problems.append(f"seq {r['seq']}: legacy row carries chain hashes (unexpected)")

    # (4) the chain after genesis
    prev = genesis.get("entry_hash")
    for r in rows[g + 1:]:
        if r.get("prev_hash") != prev:
            problems.append(f"seq {r['seq']}: prev_hash does not link to seq {r['seq'] - 1}")
        expected = entry_hash(r.get("prev_hash") or "", chain_payload(r))
        if r.get("entry_hash") != expected:
            problems.append(f"seq {r['seq']}: entry_hash does not recompute from the row")
        prev = r.get("entry_hash")

    return (not problems), problems


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

class ErasureLedger:
    """Append-only, hash-chained erasure ledger (see module docstring)."""

    def __init__(self, key_store_url: str, open: bool = True):
        self._key_store_url = key_store_url

        try:
            import psycopg  # noqa: F401
            from psycopg_pool import ConnectionPool
        except ImportError as e:
            raise RuntimeError("ErasureLedger requires psycopg and psycopg_pool") from e

        self._pool = ConnectionPool(
            self._key_store_url, min_size=1, max_size=10, open=open,
            timeout=float(os.environ.get("GRAFOMEM_DB_POOL_TIMEOUT", "10")),
            kwargs={"connect_timeout": int(os.environ.get("GRAFOMEM_DB_CONNECT_TIMEOUT", "5"))},
        )

    # ---- schema ------------------------------------------------------------

    def ensure_schema(self):
        """Self-host parity with migrations 009 + 021 (idempotent).

        Cloud installs are migration-owned and never call this with DDL rights; single-
        role self-host relies on it. The chain columns and the genesis seal are applied
        with the SAME SQL as 021 so the two paths cannot drift.
        """
        with self._pool.connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS erasure_ledger (
                    entry_id        TEXT PRIMARY KEY,
                    tenant_id       TEXT NOT NULL,
                    entry_type      TEXT NOT NULL,
                    fact_ref        INTEGER,
                    content_hash    TEXT,
                    certificate     JSONB,
                    backfill        JSONB,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
                );
            """)
            # 009 drift: `backfill` existed in the migration but not here. Converge.
            conn.execute("ALTER TABLE erasure_ledger ADD COLUMN IF NOT EXISTS backfill JSONB;")
            conn.execute(_chain_ddl())

    # ---- writes (chained) --------------------------------------------------

    def _append(self, *, entry_id: str, tenant_id: str, entry_type: str,
                fact_ref: Optional[int], content_hash: Optional[str],
                certificate: Optional[dict], backfill: Optional[dict] = None) -> str:
        """Append one chained row.

        One transaction: take the chain advisory lock (serialises writers across
        processes and pool connections), read the head, compute seq+1 and the hash,
        insert with EXPLICIT seq/prev_hash/entry_hash/created_at. Explicit seq — not a
        sequence — so a rolled-back insert leaves no gap and the verifier can require
        contiguity. Needs only the SELECT + INSERT the ledger role already holds (009).
        """
        created_at = datetime.now(timezone.utc)
        row = {
            "entry_id": entry_id, "tenant_id": tenant_id, "entry_type": entry_type,
            "fact_ref": fact_ref, "content_hash": content_hash,
            "certificate": certificate, "backfill": backfill, "created_at": created_at,
        }
        with self._pool.connection() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_CHAIN_LOCK_KEY,))
                head = conn.execute(
                    "SELECT seq, entry_hash FROM erasure_ledger ORDER BY seq DESC NULLS LAST LIMIT 1"
                ).fetchone()
                if head is None:
                    # Completely EMPTY ledger (fresh install, or wiped). There is no
                    # legacy segment to misrepresent, so self-seal with genesis([]) —
                    # INSERT only, no DDL, so it works under the ledger role in cloud.
                    # (Rows present but unsealed is different: that needs 021's backfill.)
                    conn.execute(
                        """INSERT INTO erasure_ledger
                           (entry_id, tenant_id, entry_type, seq, prev_hash, entry_hash, created_at)
                           VALUES (%s, %s, %s, 1, %s, %s, %s)""",
                        (GENESIS_ENTRY_ID, GENESIS_TENANT_ID, GENESIS_ENTRY_TYPE,
                         GENESIS_HASH, genesis_hash([]), datetime.now(timezone.utc)),
                    )
                    head = (1, genesis_hash([]))
                if head[0] is None:
                    raise RuntimeError(
                        "erasure_ledger has rows without seq: migration 021 (or "
                        "ensure_schema() in self-host) has not been applied — refusing to write."
                    )
                seq = int(head[0]) + 1
                prev = head[1]
                if prev is None:
                    raise RuntimeError(
                        f"erasure_ledger head (seq {head[0]}) has no entry_hash: legacy rows "
                        "are unsealed — apply migration 021 before writing; refusing to "
                        "extend an unanchored chain."
                    )
                digest = entry_hash(prev, chain_payload(row))
                conn.execute(
                    """INSERT INTO erasure_ledger
                       (entry_id, tenant_id, entry_type, fact_ref, content_hash, certificate,
                        backfill, created_at, seq, prev_hash, entry_hash)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (entry_id, tenant_id, entry_type, fact_ref, content_hash,
                     json.dumps(certificate) if certificate is not None else None,
                     json.dumps(backfill) if backfill is not None else None,
                     created_at, seq, prev, digest),
                )
        return entry_id

    def record_subject_erasure(self, entry_id: str, tenant_id: str, fact_ref: int,
                               content_hash: Optional[str], certificate: dict) -> str:
        """Append a subject erasure event (chained)."""
        return self._append(entry_id=entry_id, tenant_id=tenant_id,
                            entry_type="subject_erasure", fact_ref=fact_ref,
                            content_hash=content_hash, certificate=certificate)

    def record_tenant_destruction(self, entry_id: str, tenant_id: str, certificate: dict) -> str:
        """Append a tenant key destruction event (chained)."""
        return self._append(entry_id=entry_id, tenant_id=tenant_id,
                            entry_type="tenant_destruction", fact_ref=None,
                            content_hash=None, certificate=certificate)

    # ---- reads -------------------------------------------------------------

    _SELECT = ("SELECT entry_id, tenant_id, entry_type, fact_ref, content_hash, certificate, "
               "backfill, created_at, seq, prev_hash, entry_hash FROM erasure_ledger")

    def get_all_erasures(self, tenant_id: Optional[str] = None) -> list[dict]:
        """Erasure events for restore-scrub, in chain order. Excludes the genesis row."""
        query = self._SELECT + " WHERE entry_type <> %s"
        params: list[Any] = [GENESIS_ENTRY_TYPE]
        if tenant_id:
            query += " AND tenant_id = %s"
            params.append(tenant_id)
        query += " ORDER BY seq ASC NULLS LAST, created_at ASC"
        with self._pool.connection() as conn:
            from psycopg.rows import dict_row
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query, params)
                return cur.fetchall()

    def get_chain_rows(self) -> list[dict]:
        """Every row, every tenant, genesis included, in chain order — for verification."""
        with self._pool.connection() as conn:
            from psycopg.rows import dict_row
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(self._SELECT + " ORDER BY seq ASC NULLS LAST, created_at ASC")
                return cur.fetchall()

    def verify(self) -> tuple[bool, list[str]]:
        """Verify the whole chain as stored. See :func:`verify_ledger`."""
        return verify_ledger(self.get_chain_rows())

    def close(self) -> None:
        try:
            self._pool.close()
        except Exception:
            pass


def _chain_ddl() -> str:
    """The chain DDL = the shipped migration 021 itself (one source; cannot drift)."""
    from pathlib import Path
    path = Path(__file__).parent / "migrations" / "021_erasure_ledger_chain.sql"
    return path.read_text(encoding="utf-8")
