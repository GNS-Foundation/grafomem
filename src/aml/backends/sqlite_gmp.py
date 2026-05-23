"""
GRAFOMEM GMP v0.1 — persistent reference store on SQLite + sqlite-vec (v0.2 cut).

The first implementation you'd actually *run*: memories survive process restart, and
the GMP candidate filter runs *inside* the vector search instead of as a Python set
intersection. Same GMP semantics as `GMPReferenceBackend`, same conformance profile —
now backed by a file, with the tenant/valid-time predicate pushed into the index.
A `write_many` bulk-ingest fast-path batches the embedder under one transaction.

What changed in v0.2 (the metadata-column pre-filter):
  - The vec0 table carries `tenant_id`, `valid_from`, `valid_until` as *metadata
    columns* alongside the embedding. The KNN filters on them natively in one query
    (`MATCH ? AND k = N AND tenant_id = ? AND valid_until = ? ORDER BY distance`), so
    the O(N) Python candidate set and the adaptive over-fetch widening loop both go
    away. The filter fences the KNN to the candidate keyspace, so the result is the
    filtered top-k by similarity — no rank-then-filter under-retrieval (the v0.1
    filtered-ANN caveat), and on a selective tenant/as_of filter, no widening tail.
  - sqlite-vec metadata filters do not support `IS NULL`, so nulls are sentinel-
    encoded: `tenant_id = ""` for the single-tenant case, `valid_from = 0` for "valid
    from the beginning", and `valid_until = 2^62` for an open (current) interval.
    Filters use only `=`, `<=`, `>`. Times are epoch *milliseconds* (int) in the
    index; the `memories` table keeps full-precision REAL epochs for reconstruction.
    The two agree whenever valid-times differ by >= 1ms, which holds for every trace.
  - supersede closes the predecessor's interval in the index with an in-place
    `UPDATE` of its `valid_until` metadata column (verified supported in this engine).

Honesty about the engine: sqlite-vec's KNN is *brute force* — it scans every vector
and applies the metadata filter during the scan; there is no ANN index yet. The v0.2
win is correctness (exact filtered retrieval) and the removal of the Python-side
candidate scan and the widening loop, not a sublinear index. Honest delete is a real
DELETE from both tables, so a forbidden row is never a candidate, whatever the ranking.

Ranking matches the reference's exact cosine: vectors are unit-normalized before
store/query, so sqlite-vec's L2 KNN orders identically to cosine. Greedy char budget
over the filtered candidates in similarity order.

Requires: `pip install sqlite-vec`, plus a FIXED-dimension embedder (the default BGE
is 384-d). A vector index is fixed-width by construction, so the variable-dim char-bag
stub used elsewhere for embedder-invariance does not apply here. macOS note: some
Python builds disable sqlite3 extension loading; if so this falls back to apsw
(`pip install apsw`), which bundles a SQLite with extensions enabled.

Note: the vec0 schema gained columns in v0.2 and is not backward-compatible with a
v0.1 `.db` file — delete an old store file and let it rebuild.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import sqlite3
from datetime import datetime, timezone

import numpy as np

from aml.backends.gmp_reference import GMP_V02_PROFILE
from aml.backends.interface import (
    Capability, Memory, RetrieveOptions, SourceMeta, WriteOptions,
)
from aml.backends.vector_only import _default_embedder
from aml.provenance import fact_id_for_content, sign_provenance

try:
    import sqlite_vec
    from sqlite_vec import serialize_float32
except ImportError:                              # surfaced with a fix in _open()
    sqlite_vec = None

    def serialize_float32(_):                    # type: ignore[misc]
        raise RuntimeError("sqlite-vec not installed — `pip install sqlite-vec`")


# Sentinels for the vec0 metadata columns (sqlite-vec filters reject IS NULL).
OPEN_UNTIL = 2 ** 62     # open valid_until: current / chain head (no real epoch reaches this)
FROM_BEGIN = 0           # null valid_from:  valid from the beginning of time
NO_TENANT = ""           # null tenant:      single-tenant workloads


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    ref           INTEGER PRIMARY KEY AUTOINCREMENT,
    content       TEXT NOT NULL,
    written_at    TEXT NOT NULL,
    metadata      TEXT NOT NULL,
    valid_from    REAL,
    valid_until   REAL,
    tenant_id     TEXT,
    superseded_by INTEGER,
    written_by    TEXT,
    signature     BLOB,
    public_key    BLOB
);
"""

_COLS = ("ref", "content", "written_at", "metadata",
         "valid_from", "valid_until", "tenant_id", "superseded_by",
         "written_by", "signature", "public_key")

# sqlite-vec caps a KNN query's k at 4096. With the metadata filter applied in-engine,
# the query returns the filtered top-k by similarity. We bound k by the char budget
# (at most `budget` items can fit, each >= 1 char), so a budget-512 retrieve asks for
# ~513 rows, not thousands — exact, and cheap to materialize. An unbounded-budget query
# falls back to min(_KNN_MAX, N): all candidates up to the engine cap.
_KNN_MAX = 4096


def _to_ts(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    if dt.tzinfo is None:                        # naive -> assume UTC (consistent both sides)
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _from_ts(ts) -> datetime | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts is not None else None


def _normalize(v) -> np.ndarray:
    arr = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(arr))
    return arr / n if n > 0.0 else arr           # zero vector left as-is (no div-by-zero)


def _vec_tenant(tenant_id) -> str:
    return tenant_id if tenant_id is not None else NO_TENANT


def _vec_ms(dt: datetime | None) -> int | None:
    """Epoch milliseconds (int) for the index time columns — fine enough that no two
    trace events collide, exact equality for the sentinel, and well under OPEN_UNTIL."""
    ts = _to_ts(dt)
    return int(ts * 1000) if ts is not None else None


def _vec_from(dt: datetime | None) -> int:
    ms = _vec_ms(dt)
    return ms if ms is not None else FROM_BEGIN


class _Result:
    """Mimics the slice of a sqlite3 cursor the backend reads, over a buffered apsw row list."""

    def __init__(self, rows, lastrowid, rowcount):
        self._rows = rows
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _ApswConn:
    """Adapts an apsw connection to the sqlite3-style API the backend uses."""

    def __init__(self, conn):
        self._c = conn

    def execute(self, sql, params=None):
        cur = self._c.execute(sql, params) if params else self._c.execute(sql)
        rows = list(cur)                                   # apsw yields a tuple per row
        return _Result(rows, self._c.last_insert_rowid(), self._c.changes())

    def executescript(self, sql):
        self._c.execute(sql)                               # apsw runs all statements in the string

    def commit(self):
        pass                                               # apsw is autocommit outside explicit txns

    def close(self):
        self._c.close()


def _open(db_path: str):
    if sqlite_vec is None:
        raise RuntimeError("sqlite-vec not installed — `pip install sqlite-vec`")
    # Preferred path: stdlib sqlite3, if this Python was built with extension loading.
    if hasattr(sqlite3.Connection, "enable_load_extension"):
        conn = sqlite3.connect(db_path, isolation_level=None)   # autocommit -> durable per op
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn
    # Fallback (common on macOS): apsw bundles a SQLite with extension loading enabled.
    try:
        import apsw
    except ImportError as e:
        raise RuntimeError(
            "this Python's sqlite3 has no loadable-extension support (common on macOS), "
            "and apsw is not installed. Fix: `pip install apsw` — it bundles a SQLite with "
            "extensions enabled and this module auto-detects it. Then re-run."
        ) from e
    conn = apsw.Connection(db_path)
    conn.enableloadextension(True)
    conn.loadextension(sqlite_vec.loadable_path())
    conn.enableloadextension(False)
    return _ApswConn(conn)


class SQLiteGMPBackend:
    """A persistent MemoryBackend (GMP v0.2 profile) on SQLite + sqlite-vec.

    Production hardening (v0.2):
    - WAL journal mode for concurrent read/write access
    - Composite B-tree index on (tenant_id, valid_until, valid_from) for fast non-vector lookups
    - Sentinel-encoded metadata columns (no NULLs in searchable columns)
    - storage_bytes() for M5 metric reporting
    """

    __grafomem_interface__ = "0.2.0"

    def __init__(self, db_path: str = ":memory:", embed_fn=None) -> None:
        self._embed = embed_fn or _default_embedder()
        self._db_path = db_path
        self._conn = _open(db_path)

        # Production pragmas: WAL enables concurrent readers during writes;
        # synchronous=NORMAL is safe with WAL and avoids fsync on every commit.
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")

        self._dim = int(np.asarray(self._embed("dimension probe")).shape[0])
        self._conn.executescript(_SCHEMA)

        # Composite B-tree index for non-vector queries (audit by tenant, delete lookups).
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mem_tenant_valid "
            "ON memories(tenant_id, valid_until, valid_from)"
        )

        # vec0 carries the filter predicate as metadata columns alongside the embedding.
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0("
            f"embedding float[{self._dim}], "
            f"tenant_id text, valid_from integer, valid_until integer)"
        )

    # -- Storage reporting (M5, duck-typed) --------------------------------
    def storage_bytes(self) -> int | None:
        """Report the SQLite database size in bytes.  Used by M5 via duck-typed getattr."""
        if self._db_path == ":memory:":
            return None
        try:
            row = self._conn.execute(
                "SELECT page_count * page_size "
                "FROM pragma_page_count(), pragma_page_size()"
            ).fetchone()
            return int(row[0]) if row else None
        except Exception:
            return None

    # -- GMP operations ---------------------------------------------------
    def capabilities(self) -> set[Capability]:
        return set(GMP_V02_PROFILE)

    def write(self, content: str, options: WriteOptions):
        emb = _normalize(self._embed(content))
        if emb.shape[0] != self._dim:
            raise ValueError(f"embedding dim {emb.shape[0]} != store dim {self._dim}")
        written_by, signature, public_key = self._provenance(content, options)
        cur = self._conn.execute(
            "INSERT INTO memories(content, written_at, metadata, valid_from, valid_until, "
            "tenant_id, superseded_by, written_by, signature, public_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (content, datetime.now(timezone.utc).isoformat(),
             json.dumps(options.metadata or {}), _to_ts(options.valid_from),
             None, options.tenant_id, None, written_by, signature, public_key),
        )
        ref = cur.lastrowid
        # New write is current -> open interval (valid_until = OPEN_UNTIL).
        self._conn.execute(
            "INSERT INTO vec_memories(rowid, embedding, tenant_id, valid_from, valid_until) "
            "VALUES (?,?,?,?,?)",
            (ref, serialize_float32(emb.tolist()),
             _vec_tenant(options.tenant_id), _vec_from(options.valid_from), OPEN_UNTIL),
        )
        return ref

    @staticmethod
    def _provenance(content: str, options: WriteOptions):
        """Provenance columns for a write. CRYPTOGRAPHIC_PROVENANCE: a signing_key signs
        the content fact_id and records the public key (also the writer identity).
        Unsigned writes store NULLs; write_id (= the ref) and written_at still provide
        PROVENANCE on read. Returns (written_by, signature, public_key)."""
        if options.signing_key is None:
            return None, None, None
        fid = fact_id_for_content(content, options.tenant_id)
        sig, pub = sign_provenance(options.signing_key, fid)
        return pub.hex(), sig, pub

    def write_many(self, items: list[tuple[str, WriteOptions]]) -> list[int]:
        """Bulk-ingest fast-path: embed every content in ONE batched forward pass and
        insert under a single transaction. Produces the same rows as calling write() for
        each item in order (same refs, embeddings, valid-time/tenant) — only `written_at`
        is the single batch instant rather than per-item. Amortizes the embedder, which
        the latency probe shows is the entire per-write cost. Not part of the
        MemoryBackend Protocol; an optional accelerator for loading a store up front.

        items: list of (content, WriteOptions). Returns the assigned refs, in order.
        """
        if not items:
            return []
        embs = self._embed([c for c, _ in items])         # one batched forward pass -> (n, d)
        if embs.ndim != 2 or embs.shape[1] != self._dim:
            raise ValueError(f"batched embedding shape {embs.shape} != (n, {self._dim})")
        now = datetime.now(timezone.utc).isoformat()
        refs: list[int] = []
        self._conn.execute("BEGIN")
        try:
            for (content, options), row in zip(items, embs):
                emb = _normalize(row)
                written_by, signature, public_key = self._provenance(content, options)
                cur = self._conn.execute(
                    "INSERT INTO memories(content, written_at, metadata, valid_from, valid_until, "
                    "tenant_id, superseded_by, written_by, signature, public_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (content, now, json.dumps(options.metadata or {}),
                     _to_ts(options.valid_from), None, options.tenant_id, None,
                     written_by, signature, public_key),
                )
                ref = cur.lastrowid
                self._conn.execute(
                    "INSERT INTO vec_memories(rowid, embedding, tenant_id, valid_from, "
                    "valid_until) VALUES (?,?,?,?,?)",
                    (ref, serialize_float32(emb.tolist()),
                     _vec_tenant(options.tenant_id), _vec_from(options.valid_from), OPEN_UNTIL),
                )
                refs.append(ref)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return refs

    def supersede(self, old_ref, content: str, options: WriteOptions):
        new_ref = self.write(content, options)
        close_real = _to_ts(options.valid_from)
        # close the predecessor's interval at the successor's start, link the chain
        self._conn.execute(
            "UPDATE memories SET valid_until = ?, superseded_by = ? WHERE ref = ?",
            (close_real, new_ref, old_ref),
        )
        # mirror the close into the index so current/as_of filters see it (in-place UPDATE)
        close_int = int(close_real * 1000) if close_real is not None \
            else int(_to_ts(datetime.now(timezone.utc)) * 1000)
        self._conn.execute(
            "UPDATE vec_memories SET valid_until = ? WHERE rowid = ?", (close_int, old_ref),
        )
        return new_ref

    def delete(self, ref) -> bool:
        cur = self._conn.execute("DELETE FROM memories WHERE ref = ?", (ref,))
        self._conn.execute("DELETE FROM vec_memories WHERE rowid = ?", (ref,))
        return cur.rowcount > 0

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        (n,) = self._conn.execute("SELECT COUNT(*) FROM vec_memories").fetchone()
        if not n:
            return []
        qvec = serialize_float32(_normalize(self._embed(query)).tolist())
        if options.budget_tokens is None:
            budget = float("inf")
            k = min(_KNN_MAX, n)                      # unbounded budget: all candidates
        else:
            budget = options.budget_tokens
            # A char budget admits at most `budget` items (each is >= 1 char), so a
            # top-k of budget+1 is provably sufficient and never under-returns — and it
            # avoids materializing thousands of ranked rows when only a few fill the
            # budget (the min(_KNN_MAX, N) form did, inflating p50 at large N for no
            # change in result).
            k = max(1, min(_KNN_MAX, budget + 1))

        # GMP candidate predicate, pushed into the KNN as metadata conditions.
        conds, params = ["tenant_id = ?"], [_vec_tenant(options.tenant_id)]
        if options.as_of is None:
            conds.append("valid_until = ?")          # current = open interval / chain head
            params.append(OPEN_UNTIL)
        else:
            t = int(_to_ts(options.as_of) * 1000)
            conds.append("valid_from <= ?")
            params.append(t)
            conds.append("valid_until > ?")
            params.append(t)

        # Single filtered KNN: the top-k candidates by similarity (k computed above
        # from the budget; a literal in the SQL — some sqlite-vec versions reject a
        # bound k). The in-engine filter means these k are already candidates, so no
        # widening and no rank-then-filter under-retrieval.
        filt = "".join(f" AND {c}" for c in conds)
        ranked = self._conn.execute(
            f"SELECT rowid FROM vec_memories WHERE embedding MATCH ? AND k = {k}"
            f"{filt} ORDER BY distance",
            (qvec, *params),
        ).fetchall()

        # Greedy char budget over the already-filtered candidates in similarity order.
        out: list[Memory] = []
        used = 0
        for (ref,) in ranked:
            row = self._conn.execute(
                f"SELECT {', '.join(_COLS)} FROM memories WHERE ref = ?", (ref,)
            ).fetchone()
            if row is None:
                continue
            if used + len(row[1]) > budget:              # budget is the limit -> done
                break
            out.append(self._row_to_memory(row))
            used += len(row[1])
        return out

    def audit(self) -> Iterator[Memory]:
        rows = self._conn.execute(
            f"SELECT {', '.join(_COLS)} FROM memories ORDER BY ref"
        ).fetchall()
        return iter([self._row_to_memory(r) for r in rows])

    def flush(self) -> None:
        self._conn.commit()                                # no-op under autocommit; explicit anyway

    def close(self) -> None:
        self._conn.close()

    # -- internals --------------------------------------------------------
    def _row_to_memory(self, row) -> Memory:
        ref, content, written_at, metadata, vf, vu, tenant, sby, written_by, sig, pub = row
        wat = datetime.fromisoformat(written_at)
        return Memory(
            ref=ref, content=content, written_at=wat,
            metadata=json.loads(metadata), valid_from=_from_ts(vf),
            valid_until=_from_ts(vu), tenant_id=tenant, superseded_by=sby,
            source=SourceMeta(
                write_id=str(ref), written_at=wat, written_by=written_by,
                signature=bytes(sig) if sig is not None else None,
                public_key=bytes(pub) if pub is not None else None,
            ),
        )


# ============================================================================
# Self-validating smoke — `python -m aml.backends.sqlite_gmp`
#
#   1. persistence: write a chain + a second tenant, close, REOPEN the file,
#      assert current / as_of / tenant queries all survived.
#   2. conformance: run the suite on fresh in-memory stores; assert full profile.
# ============================================================================

if __name__ == "__main__":
    import tempfile
    from datetime import timedelta
    from pathlib import Path

    from aml.backends.interface import MemoryBackend
    from aml.eval.conformance import run_conformance

    print("GRAFOMEM SQLite + sqlite-vec store — GMP v0.2 (metadata pre-filter + "
          "provenance, BGE embedder)\n")

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=30)
    path = str(Path(tempfile.mkdtemp()) / "store.db")

    b = SQLiteGMPBackend(path)                             # default embedder = BGE (fixed dim)
    assert isinstance(b, MemoryBackend)
    assert b.capabilities() == set(GMP_V02_PROFILE)
    r0 = b.write("Aria lives in Rome", WriteOptions(valid_from=t0, tenant_id="A"))
    b.supersede(r0, "Aria lives in Milan", WriteOptions(valid_from=t1, tenant_id="A"))
    b.write("Bruno lives in Naples", WriteOptions(valid_from=t0, tenant_id="B"))
    b.flush()
    b.close()                                              # <- process boundary
    print(f"wrote 3 memories to {path}, closed the connection")

    b2 = SQLiteGMPBackend(path)                            # reopen the same file
    cur = [m.content for m in b2.retrieve("Where does Aria live?",
                                          RetrieveOptions(tenant_id="A", budget_tokens=512))]
    assert cur == ["Aria lives in Milan"], cur
    past = [m.content for m in b2.retrieve("Where does Aria live?",
            RetrieveOptions(as_of=t0 + timedelta(days=5), tenant_id="A", budget_tokens=512))]
    assert past == ["Aria lives in Rome"], past
    bq = [m.content for m in b2.retrieve("Where does Aria live?",
                                         RetrieveOptions(tenant_id="B", budget_tokens=512))]
    assert all("Aria" not in x for x in bq), bq
    b2.close()
    print("✓ persists across reopen   (head = Milan; as_of(t0+5d) = Rome; tenant B clean)")

    # Provenance persists: a signed write's signature survives the file round-trip.
    from aml.backends.interface import verify_provenance
    from aml.provenance import fact_id_for_content
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat,
    )

    ppath = str(Path(tempfile.mkdtemp()) / "prov.db")
    bp = SQLiteGMPBackend(ppath)
    key = Ed25519PrivateKey.generate().private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    rp = bp.write("Aria prefers tea", WriteOptions(signing_key=key, tenant_id="A"))
    bp.flush()
    bp.close()                                            # <- process boundary
    bp2 = SQLiteGMPBackend(ppath)                          # reopen
    mp = next(m for m in bp2.audit() if m.ref == rp)
    assert mp.source is not None and mp.source.write_id == str(rp)
    assert verify_provenance(mp, fact_id_for_content(mp.content, mp.tenant_id)) is True
    assert verify_provenance(mp, fact_id_for_content("Aria prefers coffee", mp.tenant_id)) is False
    bp2.close()
    print("✓ provenance persists      (signed write verifies after reopen; tamper fails)")

    print("\n  running the conformance suite (fresh :memory: stores, BGE embedder)...")
    profile = run_conformance(lambda: SQLiteGMPBackend(":memory:"),
                              name="SQLiteGMPBackend", seeds=range(2))
    print(f"  SUPPORTS {{{', '.join(sorted(c.value for c in profile.supported))}}}")
    assert profile.supported == set(GMP_V02_PROFILE), set(GMP_V02_PROFILE) - profile.supported
    assert not profile.violations, [r.capability.value for r in profile.violations]

    print("\n✓ Persistent store passes the full GMP v0.2 profile, no violations.\n"
          "  Same contract as the in-memory reference — now on a file, with the GMP filter\n"
          "  in the vector index and provenance (incl. signatures) persisted alongside.")
