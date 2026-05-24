"""
GRAFOMEM GMP v0.2 — PostgreSQL + pgvector persistent store.

The production-grade backend for GRAFOMEM Cloud. Same GMP v0.2 profile as
SQLiteGMPBackend, same 7-capability conformance — now on PostgreSQL with
pgvector HNSW indexing for sublinear approximate nearest-neighbor retrieval.

Architecture:
  - `memories` table: all memory metadata, content, provenance columns
  - `memory_embeddings` table: pgvector `vector` column with HNSW index
  - Sentinel-encoded metadata (same NULL-avoidance as the SQLite backend)
  - Bi-temporal versioning: valid_from / valid_until intervals
  - Supersession chains: linked-list via superseded_by
  - Hard-delete: DELETE from both tables (irrecoverable)
  - Multi-tenant: strict WHERE tenant_id = $1 filtering
  - Cryptographic provenance: Ed25519 signatures over fact_ids

pgvector advantages over sqlite-vec:
  - HNSW index: O(log n) approximate nearest-neighbor, not brute-force
  - Concurrent reads/writes: PostgreSQL MVCC, no WAL file locking
  - Horizontal scaling: read replicas, connection pooling (pgbouncer)
  - ACID transactions across the metadata + vector tables
  - Production monitoring: pg_stat_statements, EXPLAIN ANALYZE

Requires: `pip install grafomem[postgres]`
  → psycopg[binary]>=3.1, pgvector>=0.3, numpy>=1.24
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import numpy as np

from aml.backends.gmp_reference import GMP_V02_PROFILE
from aml.backends.interface import (
    Capability, Memory, RetrieveOptions, SourceMeta, WriteOptions,
)
from aml.backends.vector_only import _default_embedder
from aml.provenance import fact_id_for_content, sign_provenance

# Sentinels — same semantics as sqlite_gmp.py, avoiding NULLs in indexed columns.
OPEN_UNTIL_TS = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
FROM_BEGIN_TS = datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
NO_TENANT = ""


# =============================================================================
# Schema
# =============================================================================

_SCHEMA_MEMORIES = """
CREATE TABLE IF NOT EXISTS memories (
    ref           BIGSERIAL PRIMARY KEY,
    content       TEXT NOT NULL,
    written_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata      JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    valid_from    TIMESTAMPTZ,
    valid_until   TIMESTAMPTZ,
    tenant_id     TEXT,
    superseded_by BIGINT REFERENCES memories(ref),
    written_by    TEXT,
    signature     BYTEA,
    public_key    BYTEA
);
"""

_SCHEMA_EMBEDDINGS = """
CREATE TABLE IF NOT EXISTS memory_embeddings (
    ref           BIGINT PRIMARY KEY REFERENCES memories(ref) ON DELETE CASCADE,
    embedding     vector({dim}),
    tenant_id     TEXT NOT NULL DEFAULT '',
    valid_from    TIMESTAMPTZ NOT NULL DEFAULT '1970-01-01T00:00:00Z',
    valid_until   TIMESTAMPTZ NOT NULL DEFAULT '9999-12-31T23:59:59Z'
);
"""

_HNSW_INDEX = """
CREATE INDEX IF NOT EXISTS idx_emb_hnsw
    ON memory_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
"""

_TENANT_FILTER_INDEX = """
CREATE INDEX IF NOT EXISTS idx_emb_tenant
    ON memory_embeddings(tenant_id, valid_until, valid_from);
"""


# =============================================================================
# Helpers
# =============================================================================

def _normalize(v) -> np.ndarray:
    arr = np.asarray(v, dtype=np.float32)
    n = float(np.linalg.norm(arr))
    return arr / n if n > 0.0 else arr


def _vec_tenant(tenant_id: str | None) -> str:
    return tenant_id if tenant_id is not None else NO_TENANT


def _vec_from(dt: datetime | None) -> datetime:
    return dt if dt is not None else FROM_BEGIN_TS


def _vec_until(dt: datetime | None) -> datetime:
    return dt if dt is not None else OPEN_UNTIL_TS


# =============================================================================
# PostgresGMPBackend
# =============================================================================

class PostgresGMPBackend:
    """A persistent MemoryBackend (GMP v0.2 profile) on PostgreSQL + pgvector.

    Production backend for GRAFOMEM Cloud. Same interface and conformance
    profile as SQLiteGMPBackend, with HNSW indexing and PostgreSQL MVCC
    for concurrent access at scale.

    Usage:
        backend = PostgresGMPBackend(
            db_url="postgresql://user:pass@host:5432/grafomem",
            embed_fn=my_embedder,
        )
    """

    __grafomem_interface__ = "0.2.0"

    def __init__(self, db_url: str, embed_fn=None) -> None:
        try:
            import psycopg
            from pgvector.psycopg import register_vector
        except ImportError as e:
            raise RuntimeError(
                "PostgresGMPBackend requires psycopg and pgvector — "
                "`pip install grafomem[postgres]`"
            ) from e

        self._embed = embed_fn or _default_embedder()
        self._db_url = db_url

        # Probe embedding dimension
        self._dim = int(np.asarray(self._embed("dimension probe")).shape[0])

        # Connect — create the pgvector extension FIRST, then register types
        self._conn = psycopg.connect(db_url, autocommit=True)
        with self._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(self._conn)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create tables and indexes if they don't exist."""
        with self._conn.cursor() as cur:
            # Memories table (no format needed — no dimension)
            cur.execute(_SCHEMA_MEMORIES.format())

            # Embeddings table (dimension injected)
            cur.execute(_SCHEMA_EMBEDDINGS.format(dim=self._dim))

            # Indexes
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_mem_tenant_valid "
                "ON memories(tenant_id, valid_until, valid_from)"
            )
            cur.execute(_HNSW_INDEX.strip())
            cur.execute(_TENANT_FILTER_INDEX.strip())

    # -- Storage reporting (M5, duck-typed) --------------------------------

    def storage_bytes(self) -> int | None:
        """Report PostgreSQL database size in bytes."""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_database_size(current_database())"
                )
                row = cur.fetchone()
                return int(row[0]) if row else None
        except Exception:
            return None

    # -- GMP operations ---------------------------------------------------

    def capabilities(self) -> set[Capability]:
        return set(GMP_V02_PROFILE)

    def write(self, content: str, options: WriteOptions) -> int:
        emb = _normalize(self._embed(content))
        if emb.shape[0] != self._dim:
            raise ValueError(f"embedding dim {emb.shape[0]} != store dim {self._dim}")

        written_by, signature, public_key = self._provenance(content, options)

        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO memories
                   (content, written_at, metadata, valid_from, valid_until,
                    tenant_id, superseded_by, written_by, signature, public_key)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING ref""",
                (content, datetime.now(timezone.utc),
                 json.dumps(options.metadata or {}),
                 options.valid_from, None,
                 options.tenant_id, None,
                 written_by, signature, public_key),
            )
            ref = cur.fetchone()[0]

            # Insert embedding with sentinel-encoded metadata
            cur.execute(
                """INSERT INTO memory_embeddings
                   (ref, embedding, tenant_id, valid_from, valid_until)
                   VALUES (%s, %s::vector, %s, %s, %s)""",
                (ref, emb.tolist(),
                 _vec_tenant(options.tenant_id),
                 _vec_from(options.valid_from),
                 OPEN_UNTIL_TS),
            )
        return ref

    @staticmethod
    def _provenance(content: str, options: WriteOptions):
        """Compute provenance columns. Same logic as SQLiteGMPBackend."""
        if options.signing_key is None:
            return None, None, None
        fid = fact_id_for_content(content, options.tenant_id)
        sig, pub = sign_provenance(options.signing_key, fid)
        return pub.hex(), sig, pub

    def write_many(self, items: list[tuple[str, WriteOptions]]) -> list[int]:
        """Bulk-ingest: embed in one batched pass, insert under one transaction."""
        if not items:
            return []

        embs = self._embed([c for c, _ in items])
        if embs.ndim != 2 or embs.shape[1] != self._dim:
            raise ValueError(f"batched embedding shape {embs.shape} != (n, {self._dim})")

        now = datetime.now(timezone.utc)
        refs: list[int] = []

        with self._conn.transaction():
            with self._conn.cursor() as cur:
                for (content, options), row in zip(items, embs):
                    emb = _normalize(row)
                    written_by, signature, public_key = self._provenance(content, options)

                    cur.execute(
                        """INSERT INTO memories
                           (content, written_at, metadata, valid_from, valid_until,
                            tenant_id, superseded_by, written_by, signature, public_key)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           RETURNING ref""",
                        (content, now, json.dumps(options.metadata or {}),
                         options.valid_from, None,
                         options.tenant_id, None,
                         written_by, signature, public_key),
                    )
                    ref = cur.fetchone()[0]

                    cur.execute(
                        """INSERT INTO memory_embeddings
                           (ref, embedding, tenant_id, valid_from, valid_until)
                           VALUES (%s, %s::vector, %s, %s, %s)""",
                        (ref, emb.tolist(),
                         _vec_tenant(options.tenant_id),
                         _vec_from(options.valid_from),
                         OPEN_UNTIL_TS),
                    )
                    refs.append(ref)
        return refs

    def supersede(self, old_ref: Any, content: str, options: WriteOptions) -> int:
        new_ref = self.write(content, options)

        close_at = options.valid_from or datetime.now(timezone.utc)

        with self._conn.cursor() as cur:
            # Close predecessor's interval in memories table
            cur.execute(
                "UPDATE memories SET valid_until = %s, superseded_by = %s WHERE ref = %s",
                (close_at, new_ref, old_ref),
            )
            # Mirror close into embedding index for filtered retrieval
            cur.execute(
                "UPDATE memory_embeddings SET valid_until = %s WHERE ref = %s",
                (close_at, old_ref),
            )
        return new_ref

    def delete(self, ref: Any) -> bool:
        with self._conn.cursor() as cur:
            # CASCADE deletes memory_embeddings row automatically
            cur.execute("DELETE FROM memories WHERE ref = %s", (ref,))
            return cur.rowcount > 0

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        # Check if any embeddings exist
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM memory_embeddings")
            n = cur.fetchone()[0]
        if not n:
            return []

        qvec = _normalize(self._embed(query))

        if options.budget_tokens is None:
            budget = float("inf")
            k = min(4096, n)
        else:
            budget = options.budget_tokens
            k = max(1, min(4096, budget + 1))

        # Build the WHERE clause for tenant + temporal filtering
        tenant = _vec_tenant(options.tenant_id)
        conditions = ["e.tenant_id = %s"]
        params: list[Any] = [tenant]

        if options.as_of is None:
            # Current: open interval only (valid_until = sentinel)
            conditions.append("e.valid_until = %s")
            params.append(OPEN_UNTIL_TS)
        else:
            # Historical: as_of falls within [valid_from, valid_until)
            conditions.append("e.valid_from <= %s")
            params.append(options.as_of)
            conditions.append("e.valid_until > %s")
            params.append(options.as_of)

        where = " AND ".join(conditions)
        params.append(qvec.tolist())  # for the ORDER BY
        params.append(k)

        with self._conn.cursor() as cur:
            # pgvector cosine distance: 1 - cosine_similarity
            # ORDER BY embedding <=> query_vector gives nearest neighbors
            cur.execute(
                f"""SELECT e.ref
                    FROM memory_embeddings e
                    WHERE {where}
                    ORDER BY e.embedding <=> %s::vector
                    LIMIT %s""",
                params,
            )
            ranked = cur.fetchall()

        # Greedy char budget over candidates in similarity order
        out: list[Memory] = []
        used = 0
        for (ref,) in ranked:
            with self._conn.cursor() as cur:
                cur.execute(
                    """SELECT ref, content, written_at, metadata,
                              valid_from, valid_until, tenant_id,
                              superseded_by, written_by, signature, public_key
                       FROM memories WHERE ref = %s""",
                    (ref,),
                )
                row = cur.fetchone()
            if row is None:
                continue
            content = row[1]
            if used + len(content) > budget:
                break
            out.append(self._row_to_memory(row))
            used += len(content)
        return out

    def audit(self) -> Iterator[Memory]:
        with self._conn.cursor() as cur:
            cur.execute(
                """SELECT ref, content, written_at, metadata,
                          valid_from, valid_until, tenant_id,
                          superseded_by, written_by, signature, public_key
                   FROM memories ORDER BY ref"""
            )
            rows = cur.fetchall()
        return iter([self._row_to_memory(r) for r in rows])

    def flush(self) -> None:
        # PostgreSQL with autocommit — each statement is already durable
        pass

    def close(self) -> None:
        self._conn.close()

    # -- internals --------------------------------------------------------

    @staticmethod
    def _row_to_memory(row) -> Memory:
        (ref, content, written_at, metadata,
         vf, vu, tenant, sby, written_by, sig, pub) = row

        # Handle metadata: could be dict (JSONB auto-parsed) or string
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        # Normalize valid_until: sentinel means None (open interval)
        if vu is not None and vu >= OPEN_UNTIL_TS:
            vu = None

        return Memory(
            ref=ref, content=content, written_at=written_at,
            metadata=metadata or {}, valid_from=vf,
            valid_until=vu, tenant_id=tenant, superseded_by=sby,
            source=SourceMeta(
                write_id=str(ref), written_at=written_at, written_by=written_by,
                signature=bytes(sig) if sig is not None else None,
                public_key=bytes(pub) if pub is not None else None,
            ),
        )


# =============================================================================
# Self-validating smoke — `python -m aml.backends.postgres_gmp`
#
# Requires a running PostgreSQL with pgvector:
#   docker run -d --name grafomem-pg -e POSTGRES_DB=grafomem_test \
#     -e POSTGRES_USER=grafomem -e POSTGRES_PASSWORD=test \
#     -p 5432:5432 pgvector/pgvector:pg16
#
# Then: python -m aml.backends.postgres_gmp
# =============================================================================

if __name__ == "__main__":
    import os
    import sys
    from datetime import timedelta

    from aml.backends.interface import MemoryBackend

    db_url = os.environ.get(
        "GRAFOMEM_TEST_DB",
        "postgresql://grafomem:test@localhost:5432/grafomem_test"
    )

    print(f"GRAFOMEM PostgreSQL + pgvector store — GMP v0.2\n")
    print(f"  Connecting to: {db_url}")

    try:
        b = PostgresGMPBackend(db_url)
    except Exception as e:
        print(f"\n  ✗ Cannot connect: {e}")
        print("  Start PostgreSQL+pgvector first (see docstring).")
        sys.exit(1)

    assert isinstance(b, MemoryBackend)
    assert b.capabilities() == set(GMP_V02_PROFILE)
    print(f"  Embedding dim: {b._dim}")
    print(f"  Capabilities: {sorted(c.value for c in b.capabilities())}")

    # Clean slate
    with b._conn.cursor() as cur:
        cur.execute("DELETE FROM memories")

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(days=30)

    r0 = b.write("Aria lives in Rome", WriteOptions(valid_from=t0, tenant_id="A"))
    b.supersede(r0, "Aria lives in Milan", WriteOptions(valid_from=t1, tenant_id="A"))
    b.write("Bruno lives in Naples", WriteOptions(valid_from=t0, tenant_id="B"))
    b.flush()
    print("✓ wrote 3 memories (2 tenants, 1 supersession)")

    # Current retrieval
    cur_results = [m.content for m in b.retrieve(
        "Where does Aria live?",
        RetrieveOptions(tenant_id="A", budget_tokens=512))]
    assert cur_results == ["Aria lives in Milan"], cur_results
    print("✓ current retrieval          (head = Milan)")

    # Historical retrieval (as_of)
    past = [m.content for m in b.retrieve(
        "Where does Aria live?",
        RetrieveOptions(as_of=t0 + timedelta(days=5), tenant_id="A", budget_tokens=512))]
    assert past == ["Aria lives in Rome"], past
    print("✓ historical retrieval       (as_of(t0+5d) = Rome)")

    # Tenant isolation
    bq = [m.content for m in b.retrieve(
        "Where does Aria live?",
        RetrieveOptions(tenant_id="B", budget_tokens=512))]
    assert all("Aria" not in x for x in bq), bq
    print("✓ tenant isolation           (B cannot see A's data)")

    # Hard delete
    refs_before = [m.ref for m in b.audit()]
    deleted = b.delete(refs_before[0])
    assert deleted
    refs_after = [m.ref for m in b.audit()]
    assert refs_before[0] not in refs_after
    print("✓ hard delete                (ref gone from audit)")

    # Storage reporting
    size = b.storage_bytes()
    print(f"✓ storage_bytes              ({size:,} bytes)")

    # Provenance
    from aml.backends.interface import verify_provenance
    from aml.provenance import fact_id_for_content
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, NoEncryption, PrivateFormat,
        )
        key = Ed25519PrivateKey.generate().private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        rp = b.write("Aria prefers tea", WriteOptions(signing_key=key, tenant_id="A"))
        mp = next(m for m in b.audit() if m.ref == rp)
        assert mp.source is not None
        assert verify_provenance(mp, fact_id_for_content(mp.content, mp.tenant_id))
        assert not verify_provenance(mp, fact_id_for_content("Aria prefers coffee", mp.tenant_id))
        print("✓ cryptographic provenance   (Ed25519 sign + verify + tamper-detect)")
    except ImportError:
        print("⊘ cryptographic provenance   (skipped — install grafomem[crypto])")

    # Cleanup
    with b._conn.cursor() as cur:
        cur.execute("DELETE FROM memories")
    b.close()

    print(f"\n✓ PostgresGMPBackend passes all smoke checks — GMP v0.2 profile.")
    print(f"  Same contract as SQLiteGMPBackend, now on PostgreSQL + pgvector.")
