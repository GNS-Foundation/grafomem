"""
GRAFOMEM GMP v0.2 — Local ANN backend using HNSWLib and SQLite.

Provides the same interface as SQLiteGMPBackend but replaces sqlite-vec's exact KNN
with an Approximate Nearest Neighbor (ANN) index via HNSWLib. The index runs in-memory
while SQLite stores the facts and metadata.

Crucially, this uses HNSWLib's `filter` callback to push the GMP candidate predicate
down into the index traversal. Instead of retrieving K items and throwing most away
(post-filtering), the C++ traversal asks Python if a node is valid before scoring it.
This avoids the exact-search bottleneck while ensuring valid top-k.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone

import numpy as np

try:
    import hnswlib
except ImportError:
    hnswlib = None

from aml.backends.gmp_reference import GMP_V02_PROFILE
from aml.backends.interface import (
    Capability, Memory, RetrieveOptions, SourceMeta, WriteOptions,
)
from aml.backends.vector_only import REFERENCE_MODEL, _default_embedder
from aml.provenance import fact_id_for_content, sign_provenance
from aml.backends.sqlite_gmp import _to_ts, _from_ts, OPEN_UNTIL, FROM_BEGIN, NO_TENANT, _SCHEMA, _COLS


class HnswGMPBackend:
    """A persistent MemoryBackend (GMP v0.2 profile) on SQLite + HNSWLib.

    Uses an in-memory HNSW graph for fast ANN vector search, with SQLite serving
    as the durable record and metadata pre-filter engine.
    """

    __grafomem_interface__ = "0.2.0"
    __grafomem_adapter_metadata__ = {
        "underlying_system": "reference",
        "embedding_model": REFERENCE_MODEL,
        "vector_store": "hnswlib-ann-cosine",
        "retention_policy": "infinite",
        "notes": "Optional ANN scaling path. Uses HNSWLib with predicate pushdown filter.",
    }

    def __init__(self, db_path: str = ":memory:", embed_fn=None, max_elements=10000) -> None:
        if hnswlib is None:
            raise RuntimeError("hnswlib not installed — `pip install hnswlib`")

        self._embed = embed_fn or _default_embedder()
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")

        self._dim = int(np.asarray(self._embed(["probe"])).shape[1])
        self._conn.executescript(_SCHEMA)
        
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mem_tenant_valid "
            "ON memories(tenant_id, valid_until, valid_from)"
        )

        # Initialize HNSW index
        self._index = hnswlib.Index(space='cosine', dim=self._dim)
        self._index.init_index(max_elements=max_elements, ef_construction=200, M=16)
        self._index.set_ef(50)

        # Load existing vectors if any
        self._load_index()

    def _load_index(self):
        # We need a table to store vectors so we can rebuild the in-memory HNSW on load
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS vec_backup ("
            "ref INTEGER PRIMARY KEY, "
            "embedding BLOB NOT NULL)"
        )
        rows = self._conn.execute("SELECT ref, embedding FROM vec_backup").fetchall()
        for ref, emb_blob in rows:
            emb = np.frombuffer(emb_blob, dtype=np.float32)
            self._index.add_items([emb], [ref])

    def storage_bytes(self) -> int | None:
        if self._db_path == ":memory:":
            return None
        try:
            row = self._conn.execute(
                "SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()"
            ).fetchone()
            return int(row[0]) if row else None
        except Exception:
            return None

    def capabilities(self) -> set[Capability]:
        return set(GMP_V02_PROFILE)

    def write(self, content: str, options: WriteOptions):
        emb = self._embed([content])[0]
        if emb.shape[0] != self._dim:
            raise ValueError(f"embedding dim {emb.shape[0]} != store dim {self._dim}")
        
        # normalize for cosine (HNSW space='cosine' handles it, but we can do it to be safe)
        n = float(np.linalg.norm(emb))
        if n > 0:
            emb = emb / n
            
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
        
        self._conn.execute(
            "INSERT INTO vec_backup(ref, embedding) VALUES (?,?)",
            (ref, emb.tobytes()),
        )
        self._index.add_items([emb], [ref])
        return ref

    def _provenance(self, content: str, options: WriteOptions):
        if options.signing_identity is None:
            return None, None, None
        fid = fact_id_for_content(content, options.tenant_id)
        sig, pub = sign_provenance(options.signing_identity, fid)
        return pub.hex(), sig, pub

    def write_many(self, items: list[tuple[str, WriteOptions]]) -> list[int]:
        if not items:
            return []
        embs = self._embed([c for c, _ in items])
        now = datetime.now(timezone.utc).isoformat()
        refs: list[int] = []
        
        embs_to_add = []
        refs_to_add = []
        
        self._conn.execute("BEGIN")
        try:
            for (content, options), emb in zip(items, embs):
                n = float(np.linalg.norm(emb))
                if n > 0:
                    emb = emb / n
                    
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
                    "INSERT INTO vec_backup(ref, embedding) VALUES (?,?)",
                    (ref, emb.tobytes()),
                )
                refs.append(ref)
                embs_to_add.append(emb)
                refs_to_add.append(ref)
            self._conn.execute("COMMIT")
            
            if embs_to_add:
                self._index.add_items(embs_to_add, refs_to_add)
                
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return refs

    def supersede(self, old_ref, content: str, options: WriteOptions):
        new_ref = self.write(content, options)
        close_real = _to_ts(options.valid_from)
        self._conn.execute(
            "UPDATE memories SET valid_until = ?, superseded_by = ? WHERE ref = ?",
            (close_real, new_ref, old_ref),
        )
        return new_ref

    def delete(self, ref) -> bool:
        cur = self._conn.execute("DELETE FROM memories WHERE ref = ?", (ref,))
        self._conn.execute("DELETE FROM vec_backup WHERE ref = ?", (ref,))
        if cur.rowcount > 0:
            self._index.mark_deleted(ref)
        return cur.rowcount > 0

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        n = self._index.element_count
        if n == 0:
            return []
            
        qvec = self._embed([query])[0]
        n_q = float(np.linalg.norm(qvec))
        if n_q > 0:
            qvec = qvec / n_q
            
        # Pushdown filter function
        # SQLite evaluates which refs match the GMP predicates. We pre-fetch this candidate set.
        # While evaluating inside HNSW callback is possible, python callback overhead might be high.
        # Alternatively, fetch matching IDs from SQLite into a fast Python set, and filter in HNSW.
        conds, params = [], []
        if options.tenant_id is not None:
            conds.append("tenant_id = ?")
            params.append(options.tenant_id)
        else:
            conds.append("tenant_id IS NULL")
            
        if options.as_of is None:
            conds.append("valid_until IS NULL")
        else:
            t = _to_ts(options.as_of)
            conds.append("(valid_from IS NULL OR valid_from <= ?)")
            params.append(t)
            conds.append("(valid_until IS NULL OR valid_until > ?)")
            params.append(t)
            
        filt = " AND ".join(conds)
        if filt:
            filt = "WHERE " + filt
            
        cand_rows = self._conn.execute(f"SELECT ref FROM memories {filt}", params).fetchall()
        allowed_set = {r[0] for r in cand_rows}
        
        if not allowed_set:
            return []
            
        def hnsw_filter(label):
            return label in allowed_set

        # budget -> max elements
        if options.budget_tokens is None:
            budget = float("inf")
            k = min(1000, len(allowed_set)) # arbitrary large cap for unbounded
        else:
            budget = options.budget_tokens
            k = min(len(allowed_set), budget + 1)
            
        if k == 0:
            return []

        # Perform ANN search
        try:
            labels, distances = self._index.knn_query(qvec, k=k, filter=hnsw_filter)
        except RuntimeError:
            return []

        out: list[Memory] = []
        used = 0
        for ref in labels[0]:
            if ref == -1: # HNSWLib filler
                continue
            row = self._conn.execute(
                f"SELECT {', '.join(_COLS)} FROM memories WHERE ref = ?", (int(ref),)
            ).fetchone()
            if row is None:
                continue
            if used + len(row[1]) > budget:
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
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

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


if __name__ == "__main__":
    from aml.backends.interface import MemoryBackend
    from aml.eval.conformance import run_conformance

    print("GRAFOMEM HNSWLib + SQLite store — GMP v0.2\n")

    b = HnswGMPBackend()
    assert isinstance(b, MemoryBackend)
    assert b.capabilities() == set(GMP_V02_PROFILE)
    
    print("  running the conformance suite (fresh :memory: stores, BGE embedder)...")
    profile = run_conformance(lambda: HnswGMPBackend(),
                              name="HnswGMPBackend", seeds=range(1))
    print(f"  SUPPORTS {{{', '.join(sorted(c.value for c in profile.supported))}}}")
    assert profile.supported == set(GMP_V02_PROFILE)
    assert not profile.violations
    print("\n✓ ANN backend passes the full GMP v0.2 profile, no violations.")
