"""The SQLite server starts and serves without the PostgreSQL client libraries, and /health reports the installed
package version (0.4.2).

`grafomem serve -b sqlite` must not need psycopg / psycopg_pool / pgvector (the `postgres` extra): the subprocess below
makes those imports fail, builds the app exactly as `serve` does for SQLite (no db_url), runs its lifespan and does a
store round trip. /health must report importlib.metadata.version("grafomem"), not a hard-coded string.
"""
import importlib.metadata
import json
import subprocess
import sys
import textwrap

BLOCKED = ("psycopg", "psycopg_pool", "pgvector", "psycopg2")

SCRIPT = textwrap.dedent("""
    import json, sys
    for name in %r:
        sys.modules[name] = None        # `import <name>` (and any submodule) now raises ImportError
    from fastapi.testclient import TestClient
    from aml.backends.sqlite_gmp import SQLiteGMPBackend
    from aml.backends.vector_only import _stub_embedder
    from aml.server.app import create_app
    batch = _stub_embedder()
    embed = lambda t: batch([t])[0] if isinstance(t, str) else batch(t)
    app = create_app(lambda: SQLiteGMPBackend(db_path=":memory:", embed_fn=embed), auth_mode="token", tokens={"tok": "tenant_a"})
    h = {"Authorization": "Bearer tok"}
    with TestClient(app) as c:
        health = c.get("/health")
        sid = c.post("/v1/stores", headers=h, json={}).json()["store_id"]
        w = c.post(f"/v1/stores/{sid}/write", headers=h, json={"content": "a fact", "options": {}})
        a = c.get(f"/v1/stores/{sid}/audit", headers=h)
    print(json.dumps({"health": health.status_code, "write": w.status_code, "audit": [m["content"] for m in a.json()["memories"]]}))
""") % (BLOCKED,)


def test_sqlite_server_starts_without_the_postgres_client_libraries():
    r = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out == {"health": 200, "write": 200, "audit": ["a fact"]}, out


def test_health_reports_the_installed_package_version():
    from fastapi.testclient import TestClient
    from aml.backends.sqlite_gmp import SQLiteGMPBackend
    from aml.backends.vector_only import _stub_embedder
    from aml.server.app import create_app
    batch = _stub_embedder()
    app = create_app(lambda: SQLiteGMPBackend(db_path=":memory:", embed_fn=lambda t: batch([t])[0] if isinstance(t, str) else batch(t)))
    with TestClient(app) as c:
        body = c.get("/health").json()
    assert body["version"] == importlib.metadata.version("grafomem"), body
