"""
GRAFOMEM Cloud — Sandbox launcher.

Starts the GRAFOMEM Cloud server with PostgreSQL (pgvector) backend.

Usage:
    DATABASE_URL=postgresql://grafomem:grafomem@localhost:5433/grafomem \
    python3 tests/sandbox_server.py
"""
import os
import uvicorn


db_url = os.environ.get(
    "DATABASE_URL",
    "postgresql://grafomem:grafomem@localhost:5433/grafomem",
)

# Generate a deterministic Ed25519 signing key for sandbox mode.
# This enables the erasure certificate signing path (P0-6).
# In production, this would be a securely stored key.
if not os.environ.get("ERASURE_SIGNING_KEY"):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    _sandbox_key = Ed25519PrivateKey.generate()
    # Ed25519 private key seed is 32 bytes
    _seed = _sandbox_key.private_bytes_raw()
    os.environ["ERASURE_SIGNING_KEY"] = _seed.hex()
    print(f"   🔑 Generated sandbox Ed25519 signing key: {_seed.hex()[:16]}...")


def _pg_factory():
    """Create a PostgresGMPBackend instance pointing at the sandbox DB."""
    from aml.backends.postgres_gmp import PostgresGMPBackend
    return PostgresGMPBackend(db_url)


from aml.server.app import create_app  # noqa: E402

app = create_app(
    backend_factory=_pg_factory,
    db_url=db_url,
    portal_secret_key="sandbox-secret-key-2026",
)

if __name__ == "__main__":
    print(f"\n🚀 GRAFOMEM Cloud — Sandbox Mode")
    print(f"   Database: {db_url.split('@')[1] if '@' in db_url else db_url}")
    print(f"   Backend:  PostgreSQL + pgvector")
    print(f"   Portal:   http://localhost:8080/portal")
    print(f"   API Docs: http://localhost:8080/docs\n")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
