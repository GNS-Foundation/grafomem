"""Store audit is scoped to the caller's tenant and to the store (0.4.1).

Non-vacuity probes, on SQLite (one database file shared by every store of a server, as `grafomem serve -b sqlite --db`)
AND on PostgreSQL (one shared `memories` table):
  1. tenant B's REST audit of B's own store never contains tenant A's memories;
  2. a tenant's audit of store X never contains the rows of its other store Y;
  3. the MCP tool `list_memories` never returns a tenant's memories (it lists the untenanted namespace its own
     write_memory / retrieve_memories use);
  4. no HTTP or MCP handler calls the unscoped `backend.audit()` (static scan), and a planted unscoped call in a copy
     of a handler makes the scan fail.
"""
import ast
import asyncio
import json
import pathlib
import shutil

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "aml"
SECRET_A = "tenant-a-confidential-fact"

# Modules allowed to call the unscoped audit(): backend implementations and their self-tests, the conformance suite,
# the GMP reference wire binding (a conformance harness without tenants), and the scoped helper's per-store fallback.
ALLOWED = ("backends/", "eval/", "wire.py")


def unscoped_audit_calls(root: pathlib.Path) -> list[str]:
    """Every `<expr>.audit()` call with no argument outside the allowed modules, as 'path:line'."""
    hits = []
    for f in sorted(root.rglob("*.py")):
        rel = f.relative_to(root).as_posix()
        if rel.startswith(ALLOWED):
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "audit"
                    and not node.args and not node.keywords):
                hits.append(f"{rel}:{node.lineno}")
    return hits


# ---- backends ------------------------------------------------------------------------------------------------

def _stub():
    from aml.backends.vector_only import _stub_embedder
    batch = _stub_embedder()
    return lambda t: batch([t])[0] if isinstance(t, str) else batch(t)


@pytest.fixture(params=["sqlite", "postgres"])
def factory(request, tmp_path):
    if request.param == "sqlite":
        from aml.backends.sqlite_gmp import SQLiteGMPBackend
        db = str(tmp_path / "shared.db")
        embed = _stub()
        return lambda: SQLiteGMPBackend(db_path=db, embed_fn=embed)
    from aml.backends.postgres_gmp import PostgresGMPBackend
    from tests.conftest import TEST_DB_URL
    try:
        PostgresGMPBackend(TEST_DB_URL).close()
    except Exception as e:   # pragma: no cover - CI has the pgvector service
        pytest.skip(f"PostgreSQL not available: {e}")
    return lambda: PostgresGMPBackend(TEST_DB_URL)


def _client(factory):
    from fastapi.testclient import TestClient
    from aml.server.app import create_app
    app = create_app(factory, auth_mode="token", tokens={"tok-a": "tenant_a", "tok-b": "tenant_b"})
    return TestClient(app)


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


def _store(c, tok):
    r = c.post("/v1/stores", headers=_h(tok), json={})
    assert r.status_code == 200, r.text
    return r.json()["store_id"]


def _write(c, tok, store, content):
    r = c.post(f"/v1/stores/{store}/write", headers=_h(tok), json={"content": content, "options": {}})
    assert r.status_code == 200, r.text


def _audit(c, tok, store):
    r = c.get(f"/v1/stores/{store}/audit", headers=_h(tok))
    assert r.status_code == 200, r.text
    return r.json()["memories"]


# ---- 1. tenant scope ----------------------------------------------------------------------------------------------

def test_store_audit_never_returns_another_tenants_memories(factory):
    c = _client(factory)
    sa, sb = _store(c, "tok-a"), _store(c, "tok-b")
    _write(c, "tok-a", sa, SECRET_A)
    _write(c, "tok-b", sb, "tenant-b-own-fact")
    b = _audit(c, "tok-b", sb)
    assert [m["content"] for m in b] == ["tenant-b-own-fact"], b
    assert all(m.get("tenant_id") == "tenant_b" for m in b), b
    a = _audit(c, "tok-a", sa)
    assert [m["content"] for m in a] == [SECRET_A], "the owner still audits its own store"
    assert c.get(f"/v1/stores/{sa}/audit", headers=_h("tok-b")).status_code == 403, "another tenant's store stays refused"


# ---- 2. store scope -------------------------------------------------------------------------------------------------

def test_store_audit_never_returns_the_tenants_other_stores(factory):
    c = _client(factory)
    x, y = _store(c, "tok-a"), _store(c, "tok-a")
    _write(c, "tok-a", x, "fact-in-store-x")
    _write(c, "tok-a", y, "fact-in-store-y")
    assert [m["content"] for m in _audit(c, "tok-a", x)] == ["fact-in-store-x"]
    assert [m["content"] for m in _audit(c, "tok-a", y)] == ["fact-in-store-y"]


# ---- 3. MCP list_memories ---------------------------------------------------------------------------------------------

def _mcp(server, name, args):
    out = asyncio.run(server._test_call_tool(name, args))
    return json.loads(out[0].text)


def test_mcp_list_memories_never_returns_a_tenants_memories(factory):
    from aml.server.mcp import create_mcp_server
    c = _client(factory)
    sa = _store(c, "tok-a")
    _write(c, "tok-a", sa, SECRET_A)
    server = create_mcp_server(factory)
    _mcp(server, "write_memory", {"content": "mcp-own-fact"})
    listed = [m["content"] for m in _mcp(server, "list_memories", {})["memories"]]
    assert SECRET_A not in listed, listed
    assert "mcp-own-fact" in listed, listed


# ---- 4. guard -----------------------------------------------------------------------------------------------------------

def test_no_handler_calls_the_unscoped_audit():
    assert unscoped_audit_calls(SRC) == []


def test_a_planted_unscoped_audit_call_fails_the_guard(tmp_path):
    copy = tmp_path / "aml"
    shutil.copytree(SRC, copy, ignore=shutil.ignore_patterns("__pycache__"))
    assert unscoped_audit_calls(copy) == [], "the copy starts clean"
    app = copy / "server" / "app.py"
    s = app.read_text(encoding="utf-8")
    s += "\n\ndef _planted(entry):\n    return list(entry.backend.audit())\n"
    app.write_text(s, encoding="utf-8")
    hits = unscoped_audit_calls(copy)
    assert len(hits) == 1 and hits[0].startswith("server/app.py:"), hits
