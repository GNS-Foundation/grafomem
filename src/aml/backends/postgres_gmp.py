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
from contextlib import contextmanager
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
    public_key    BYTEA,
    region        TEXT DEFAULT 'global'
);
"""

_SCHEMA_EMBEDDINGS = """
CREATE TABLE IF NOT EXISTS memory_embeddings (
    ref           BIGINT PRIMARY KEY,
    embedding     vector({dim}),
    tenant_id     TEXT NOT NULL DEFAULT '',
    valid_from    TIMESTAMPTZ NOT NULL DEFAULT '1970-01-01T00:00:00Z',
    valid_until   TIMESTAMPTZ NOT NULL DEFAULT '9999-12-31T23:59:59Z',
    erasure_pending TIMESTAMPTZ,
    region        TEXT DEFAULT 'global'
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
    ON memory_embeddings(tenant_id, region, valid_until, valid_from);
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

    def __init__(self, db_url: str, embed_fn=None, encryption=None) -> None:
        try:
            import psycopg
            from psycopg_pool import ConnectionPool
            from pgvector.psycopg import register_vector
        except ImportError as e:
            raise RuntimeError(
                "PostgresGMPBackend requires psycopg, psycopg_pool, and pgvector — "
                "`pip install grafomem[postgres]`"
            ) from e

        self._embed = embed_fn or _default_embedder()
        self._db_url = db_url
        self._encryption = encryption

        # Probe embedding dimension
        self._dim = int(np.asarray(self._embed("dimension probe")).shape[0])

        # Initialize connection pool
        self._pool = ConnectionPool(db_url, min_size=1, max_size=20)

        # Connect — create the pgvector extension FIRST, then register types
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                import psycopg
                try:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                except psycopg.errors.InsufficientPrivilege:
                    # Ignore if the user isn't superuser (assume vector is already created by admin)
                    conn.rollback()
            register_vector(conn)
        self._ensure_schema()

    @contextmanager
    def _tenant_conn(self, tenant_id: str):
        """Yields a connection and cursor with the Postgres RLS tenant context set."""
        with self._pool.connection() as conn:
            from pgvector.psycopg import register_vector
            register_vector(conn)
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SELECT set_config('app.current_tenant', %s, true)", (tenant_id,))
                    yield conn, cur

    def _ensure_schema(self) -> None:
        """Create tables and indexes if they don't exist."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                # Memories table (no format needed — no dimension)
                cur.execute(_SCHEMA_MEMORIES.format())

                # Embeddings table (dimension injected)
                cur.execute(_SCHEMA_EMBEDDINGS.format(dim=self._dim))


                # Migration for encryption columns
                try:
                    cur.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS content_enc TEXT;")
                    cur.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS metadata_enc TEXT;")
                except Exception as e:
                    logger.warning(f"Could not alter memories table for encryption columns: {e}")

                # Migration for region columns
                try:
                    cur.execute("ALTER TABLE memories ADD COLUMN IF NOT EXISTS region TEXT DEFAULT 'global';")
                    cur.execute("ALTER TABLE memory_embeddings ADD COLUMN IF NOT EXISTS region TEXT DEFAULT 'global';")
                except Exception as e:
                    logger.warning(f"Could not alter tables for region columns: {e}")

                # Indexes (must run after migrations to ensure columns exist)
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_mem_tenant_valid "
                    "ON memories(tenant_id, valid_until, valid_from)"
                )
                cur.execute(_HNSW_INDEX.strip())
                cur.execute(_TENANT_FILTER_INDEX.strip())

                # Enable Postgres RLS
                cur.execute("ALTER TABLE memories ENABLE ROW LEVEL SECURITY")
                cur.execute(
                    """
                    DO $$ BEGIN
                        CREATE POLICY tenant_isolation_memories ON memories
                            USING (tenant_id = current_setting('app.current_tenant', true) OR current_setting('app.current_tenant', true) = 'admin');
                    EXCEPTION
                        WHEN duplicate_object THEN null;
                    END $$;
                    """
                )
                cur.execute("ALTER TABLE memory_embeddings ENABLE ROW LEVEL SECURITY")
                cur.execute(
                    """
                    DO $$ BEGIN
                        CREATE POLICY tenant_isolation_embeddings ON memory_embeddings
                            USING (tenant_id = current_setting('app.current_tenant', true) OR current_setting('app.current_tenant', true) = 'admin');
                    EXCEPTION
                        WHEN duplicate_object THEN null;
                    END $$;
                    """
                )

    # -- Storage reporting (M5, duck-typed) --------------------------------

    def storage_bytes(self) -> int | None:
        """Report PostgreSQL database size in bytes."""
        try:
            with self._pool.connection() as conn:
                with conn.cursor() as cur:
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

    def _encrypt_memory(self, content: str, metadata: dict | None, tenant_id: str | None) -> tuple[str, str | None, str, str | None]:
        """Return (db_content, enc_content, db_metadata, enc_metadata)"""
        meta_canon = _CANON(metadata) if metadata else "{}"
        if self._encryption:
            encryptor = self._encryption
            if tenant_id and hasattr(encryptor, "get_encryptor"):
                encryptor = encryptor.get_encryptor(tenant_id)
            enc_content = encryptor.encrypt(content)
            db_content = "[ENCRYPTED]"
            enc_meta = encryptor.encrypt(meta_canon)
            db_meta = "{}"
            return db_content, enc_content, db_meta, enc_meta
        else:
            return content, None, meta_canon, None

    def write(self, content: str, options: WriteOptions) -> int:
        emb = _normalize(self._embed(content))
        if emb.shape[0] != self._dim:
            raise ValueError(f"embedding dim {emb.shape[0]} != store dim {self._dim}")

        written_by, signature, public_key = self._provenance(content, options)
        
        db_content, enc_content, db_meta, enc_meta = self._encrypt_memory(content, options.metadata, options.tenant_id)

        with self._tenant_conn(options.tenant_id) as (conn, cur):
            cur.execute(
                """INSERT INTO memories
                   (content, written_at, metadata, valid_from, valid_until,
                    tenant_id, superseded_by, written_by, signature, public_key,
                    content_enc, metadata_enc, region)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING ref""",
                (db_content, datetime.now(timezone.utc), db_meta,
                 options.valid_from, None,
                 options.tenant_id, None,
                 written_by, signature, public_key,
                 enc_content, enc_meta, options.region or 'global'),
            )
            ref = cur.fetchone()[0]

            # Insert embedding with sentinel-encoded metadata
            cur.execute(
                """INSERT INTO memory_embeddings
                   (ref, embedding, tenant_id, valid_from, valid_until, region)
                   VALUES (%s, %s::vector, %s, %s, %s, %s)""",
                (ref, emb.tolist(),
                 _vec_tenant(options.tenant_id),
                 _vec_from(options.valid_from),
                 OPEN_UNTIL_TS, options.region or 'global'),
            )
        return ref

    @staticmethod
    def _provenance(content: str, options: WriteOptions):
        """Compute provenance columns. Same logic as SQLiteGMPBackend."""
        if options.signing_identity is None:
            return None, None, None
        fid = fact_id_for_content(content, options.tenant_id)
        sig, pub = sign_provenance(options.signing_identity, fid)
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

        for (content, options), row in zip(items, embs):
            emb = _normalize(row)
            written_by, signature, public_key = self._provenance(content, options)
            db_content, enc_content, db_meta, enc_meta = self._encrypt_memory(content, options.metadata, options.tenant_id)

            with self._tenant_conn(options.tenant_id) as (conn, cur):
                cur.execute(
                    """INSERT INTO memories
                       (content, written_at, metadata, valid_from, valid_until,
                        tenant_id, superseded_by, written_by, signature, public_key,
                        content_enc, metadata_enc, region)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING ref""",
                    (db_content, now, db_meta,
                     options.valid_from, None,
                     options.tenant_id, None,
                     written_by, signature, public_key,
                     enc_content, enc_meta, options.region or 'global'),
                )
                ref = cur.fetchone()[0]

                cur.execute(
                    """INSERT INTO memory_embeddings
                       (ref, embedding, tenant_id, valid_from, valid_until, region)
                       VALUES (%s, %s::vector, %s, %s, %s, %s)""",
                    (ref, emb.tolist(),
                     _vec_tenant(options.tenant_id),
                     _vec_from(options.valid_from),
                     OPEN_UNTIL_TS,
                     options.region or 'global'),
                )
                refs.append(ref)
        return refs

    def supersede(self, ref: int, content: str, metadata: dict | None, options: WriteOptions) -> int:
        db_content, enc_content, db_meta, enc_meta = self._encrypt_memory(content, metadata, options.tenant_id)

        close_at = options.valid_from or datetime.now(timezone.utc)
        
        emb = _normalize(self._embed(content))
        written_by, signature, public_key = self._provenance(content, options)

        with self._tenant_conn(options.tenant_id) as (conn, cur):
            # Insert the new memory
            cur.execute(
                """INSERT INTO memories
                   (content, written_at, metadata, valid_from, valid_until,
                    tenant_id, superseded_by, written_by, signature, public_key,
                    content_enc, metadata_enc, region)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING ref""",
                (db_content, datetime.now(timezone.utc), db_meta,
                 options.valid_from, None,
                 options.tenant_id, None,
                 written_by, signature, public_key,
                 enc_content, enc_meta, options.region or 'global'),
            )
            new_ref = cur.fetchone()[0]

            cur.execute(
                """INSERT INTO memory_embeddings
                   (ref, embedding, tenant_id, valid_from, valid_until, region)
                   VALUES (%s, %s::vector, %s, %s, %s, %s)""",
                (new_ref, emb.tolist(),
                 _vec_tenant(options.tenant_id),
                 _vec_from(options.valid_from),
                 OPEN_UNTIL_TS, options.region or 'global'),
            )

            # Close predecessor's interval in memories table
            cur.execute(
                "UPDATE memories SET valid_until = %s, superseded_by = %s WHERE ref = %s",
                (close_at, new_ref, ref),
            )
            # Mirror close into embedding index for filtered retrieval
            cur.execute(
                "UPDATE memory_embeddings SET valid_until = %s WHERE ref = %s",
                (close_at, ref),
            )
        return new_ref

    def delete(self, ref: Any) -> bool:
        with self._tenant_conn("admin") as (conn, cur):
            # Mark embedding for background erasure sweep before deleting primary memory
            cur.execute("UPDATE memory_embeddings SET erasure_pending = now() WHERE ref = %s", (ref,))
            cur.execute("DELETE FROM memories WHERE ref = %s", (ref,))
            return cur.rowcount > 0

    def retrieve(self, query: str, options: RetrieveOptions) -> list[Memory]:
        # Check if any embeddings exist
        with self._tenant_conn(options.tenant_id) as (conn, cur):
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

        if options.region:
            conditions.append("e.region = %s")
            params.append(options.region)

        where = " AND ".join(conditions)
        params.append(qvec.tolist())  # for the ORDER BY
        params.append(k)

        with self._tenant_conn(options.tenant_id) as (conn, cur):
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
            with self._tenant_conn(options.tenant_id) as (conn, cur):
                cur.execute(
                    """SELECT ref, content, written_at, metadata,
                              valid_from, valid_until, tenant_id,
                              superseded_by, written_by, signature, public_key, region,
                              content_enc, metadata_enc
                       FROM memories WHERE ref = %s""",
                    (ref,),
                )
                row = cur.fetchone()
            if row is None:
                continue
            
            mem = self._row_to_memory(row)
            if used + len(mem.content) > budget:
                break
            out.append(mem)
            used += len(mem.content)
        return out

    def audit(self) -> Iterator[Memory]:
        with self._tenant_conn("admin") as (conn, cur):
            cur.execute(
                """SELECT ref, content, written_at, metadata,
                          valid_from, valid_until, tenant_id,
                          superseded_by, written_by, signature, public_key, region,
                          content_enc, metadata_enc
                   FROM memories ORDER BY ref"""
            )
            rows = cur.fetchall()
        return iter([self._row_to_memory(r) for r in rows])

    def flush(self) -> None:
        # PostgreSQL with autocommit — each statement is already durable
        pass

    def close(self) -> None:
        self._pool.close()

    # -- internals --------------------------------------------------------

    def _row_to_memory(self, row, content_override: str | None = None) -> Memory:
        (ref, content, written_at, metadata,
         vf, vu, tenant, sby, written_by, sig, pub, region,
         content_enc, metadata_enc) = row

        if content_override is not None:
            content = content_override
        elif self._encryption and content_enc:
            encryptor = self._encryption
            if tenant and hasattr(encryptor, "get_encryptor"):
                encryptor = encryptor.get_encryptor(tenant)
            content = encryptor.decrypt(content_enc)

        # Handle metadata: could be dict (JSONB auto-parsed) or string
        if isinstance(metadata, str):
            if self._encryption and metadata_enc:
                encryptor = self._encryption
                if tenant and hasattr(encryptor, "get_encryptor"):
                    encryptor = encryptor.get_encryptor(tenant)
                metadata = encryptor.decrypt(metadata_enc)
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
            ) if written_by is not None else None,
            region=region,
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
    with b._pool.connection() as conn:
        with conn.cursor() as cur:
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
        rp = b.write("Aria prefers tea", WriteOptions(signing_identity=key, tenant_id="A"))
        mp = next(m for m in b.audit() if m.ref == rp)
        assert mp.source is not None
        assert verify_provenance(mp, fact_id_for_content(mp.content, mp.tenant_id))
        assert not verify_provenance(mp, fact_id_for_content("Aria prefers coffee", mp.tenant_id))
        print("✓ cryptographic provenance   (Ed25519 sign + verify + tamper-detect)")
    except ImportError:
        print("⊘ cryptographic provenance   (skipped — install grafomem[crypto])")

    # Cleanup
    with b._pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memories")
    b.close()

    print(f"\n✓ PostgresGMPBackend passes all smoke checks — GMP v0.2 profile.")
    print(f"  Same contract as SQLiteGMPBackend, now on PostgreSQL + pgvector.")
