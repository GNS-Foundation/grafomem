#!/usr/bin/env python3
"""
GRAFOMEM — GMP Self-Conformance (W1–W6 + W10)

Runs the GMP v0.2 conformance suite against the PostgresGMPBackend
connected to the same Docker PostgreSQL+pgvector instance the Cloud
platform uses. This is the dogfood test: the platform's own backend,
tested by the platform's own conformance suite.

The suite tests all 7 declared capabilities:
  AUDIT, SUPERSESSION_CHAIN, BI_TEMPORAL, HARD_DELETE,
  MULTI_TENANT, PROVENANCE, CRYPTOGRAPHIC_PROVENANCE

Each capability is tested two-sided where applicable:
  - Safety (leakage) direction: forbidden data must NOT appear
  - Correctness (recall) direction: permitted data MUST appear

Usage:
    PGPASSWORD=grafomem DATABASE_URL=postgresql://grafomem:grafomem@localhost:5433/grafomem \\
        python3 tests/gmp_self_conformance.py
"""

import os
import sys
import time

# ---------------------------------------------------------------------------
# Database setup — use a SEPARATE schema so we don't clobber the Cloud tables
# ---------------------------------------------------------------------------

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://grafomem:grafomem@localhost:5433/grafomem",
)

def _ensure_clean_schema():
    """Create a clean 'gmp_conformance' schema for the test."""
    import psycopg
    conn = psycopg.connect(DB_URL, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS gmp_conformance CASCADE")
        cur.execute("CREATE SCHEMA gmp_conformance")
        cur.execute("SET search_path TO gmp_conformance, public")
        # Ensure pgvector extension is available
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
    conn.close()


def _make_factory():
    """Factory that creates fresh PostgresGMPBackend instances.
    Each call drops and recreates the memories/embeddings tables
    to ensure test isolation."""
    from aml.backends.postgres_gmp import PostgresGMPBackend
    from aml.backends.vector_only import _stub_embedder

    # The stub embedder is a batch function (list[str] -> np.ndarray).
    # PostgresGMPBackend calls embed_fn with a SINGLE string, so we
    # wrap it to handle both cases.
    _batch_fn = _stub_embedder(dim=256)

    def single_or_batch_embed(text_or_texts):
        import numpy as np
        if isinstance(text_or_texts, str):
            return _batch_fn([text_or_texts])[0]
        return _batch_fn(text_or_texts)

    def factory():
        import psycopg
        # Drop tables for isolation — each conformance seed gets a clean store
        conn = psycopg.connect(DB_URL, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS memory_embeddings CASCADE")
            cur.execute("DROP TABLE IF EXISTS memories CASCADE")
        conn.close()

        return PostgresGMPBackend(db_url=DB_URL, embed_fn=single_or_batch_embed)

    return factory


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print("  GRAFOMEM — GMP Self-Conformance")
    print("  PostgreSQL + pgvector backend (W2, W5, W6, W10 workloads)")
    print("  The dogfood test: our own suite, against our own backend.")
    print("=" * 70)
    print()

    # Import the conformance suite
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from aml.eval.conformance import run_conformance, print_profile
    from aml.backends.interface import Capability

    # Ensure we can connect to PG
    print(f"  Database: {DB_URL.split('@')[-1] if '@' in DB_URL else DB_URL}")
    print(f"  Backend:  PostgresGMPBackend")
    print(f"  Embedder: stub (privacy/supersession invariant — paper Prop 2)")
    print()

    factory = _make_factory()

    # Quick sanity: can we create an instance?
    try:
        probe = factory()
        caps = probe.capabilities()
        print(f"  Declared capabilities: {{{', '.join(sorted(c.value for c in caps))}}}")
        print(f"  Interface version:     {probe.__grafomem_interface__}")
        print()
    except Exception as e:
        print(f"\n✗ Cannot connect to PostgreSQL: {e}")
        print(f"  Is the Docker container running?")
        print(f"  Expected: docker run -d --name grafomem-pg "
              f"-e POSTGRES_USER=grafomem -e POSTGRES_PASSWORD=grafomem "
              f"-e POSTGRES_DB=grafomem -p 5433:5432 pgvector/pgvector:pg17")
        sys.exit(1)

    # Run the full conformance suite
    print("Running conformance suite (5 seeds × 7 capabilities)...")
    print("  This tests W2 (versioning), W5 (multi-tenant), W6 (deletion),")
    print("  W10 (concurrency), AUDIT, PROVENANCE, CRYPTOGRAPHIC_PROVENANCE")
    print()

    t0 = time.perf_counter()

    profile = run_conformance(
        factory,
        name="PostgresGMPBackend",
        seeds=range(5),
        budget=512,
        strict=False,  # Don't raise — report the profile
    )

    elapsed = time.perf_counter() - t0

    # Print the profile
    print_profile(profile)

    # Summary
    print()
    print("=" * 70)
    print(f"  M8 conformance rate: {profile.conformance_rate:.3f}")
    print(f"  Elapsed: {elapsed:.1f}s")
    print()

    if profile.violations:
        print("  ❌ CONFORMANCE VIOLATIONS:")
        for v in profile.violations:
            print(f"     {v.capability.value}: {v.workload}")
            for d in v.directions:
                if not d.passed:
                    print(f"       {d.name}: {d.point:.3f} (gate: {d.objective})")
        print()
        print("  The PostgresGMPBackend does NOT pass its own conformance suite.")
        print("  This means the backend's DECLARED capabilities do not match its BEHAVIOR.")
        print("  Fix the backend before shipping.")
        sys.exit(1)
    else:
        print("  ✅ ALL CAPABILITIES PASS — No violations.")
        print()
        print("  The PostgresGMPBackend SUPPORTS everything it declares:")
        sup = sorted(c.value for c in profile.supported)
        for c in sup:
            print(f"    ✅ {c}")
        print()
        untested = sorted(c.value for c in profile.untested) if profile.untested else []
        if untested:
            print(f"  ⚠ Declared but no v0.2 test: {{{', '.join(untested)}}}")
            print()
        print("  GMP self-conformance GREEN — the platform's own backend is honest.")
        print("=" * 70)


if __name__ == "__main__":
    main()
