"""MCP audit batch A + process-per-tenant — on REAL backends (findings 1, 2, 4, 9–14).

The previous MCP tests ran on a MockBackend that implemented get(); every real backend
lacked it and the tools raised. These tests use the shipped SQLite and Postgres backends
through the actual MCP server object, so what passes here is what a client gets.

Must-fail discipline: each test pins a guard that the pre-batch-A code violates.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

from aml.backends.interface import Capability
from aml.backends.vector_only import _stub_embedder
from aml.server.mcp import create_mcp_server

DB_URL = os.environ.get("GRAFOMEM_DB_URL")


def _embed(dim=256):
    batch = _stub_embedder(dim)
    return lambda t: batch([t])[0] if isinstance(t, str) else batch(t)


def _sqlite_factory(path):
    from aml.backends.sqlite_gmp import SQLiteGMPBackend
    return lambda: SQLiteGMPBackend(db_path=path, embed_fn=_embed())


def _pg_factory():
    from aml.backends.postgres_gmp import PostgresGMPBackend
    # the test database's vector columns are 384-dim (BGE); match them
    return lambda: PostgresGMPBackend(DB_URL, embed_fn=_embed(384))


async def _call(server, tool, args=None):
    res = await server._test_call_tool(tool, args or {})
    return json.loads(res[0].text)


@pytest.fixture
def sqlite_path(tmp_path):
    return str(tmp_path / "live.db")


# ── finding 2: the basic flow, on a real backend ────────────────────────────

@pytest.mark.asyncio
async def test_real_sqlite_write_retrieve_delete_verify(sqlite_path, tmp_path):
    server = create_mcp_server(_sqlite_factory(sqlite_path), tenant_id="t-a",
                               scratch_factory=_sqlite_factory(str(tmp_path / "scratch.db")))
    ref = (await _call(server, "write_memory", {"content": "the canary sings at dawn"}))["ref"]

    got = await _call(server, "retrieve_memories", {"query": "canary"})
    assert any(m["ref"] == ref for m in got["memories"])

    d = await _call(server, "delete_memory", {"ref": ref})
    assert d["deleted"] is True                          # 0.4.2: AttributeError 'get'
    assert d["sealed_probe"]["gone_from_retrieval"] is True
    assert d["certificate"] is None
    assert d["certificate_status"] == "not_issued: no erasure service in this MCP process"

    v = await _call(server, "verify_erasure", {"ref": ref, "reverify": True, "probe_query": "canary"})
    assert v["status"] == "NOT_FOUND"                    # gone, and no certificate: nothing claimed
    assert v["storage_check"]["row_present"] is False


# ── finding 9: process-per-tenant isolation, on a real multi-tenant backend ──

@pytest.fixture
def pg():
    if not DB_URL:
        pytest.skip("GRAFOMEM_DB_URL not set")
    return _pg_factory()


@pytest.mark.asyncio
async def test_unbound_process_refuses_a_multi_tenant_backend(sqlite_path):
    # SQLite declares MULTI_TENANT too (not just Postgres): unbound must fail closed.
    server = create_mcp_server(_sqlite_factory(sqlite_path))  # no tenant_id
    with pytest.raises(RuntimeError, match="MULTI_TENANT but no tenant is bound"):
        await server._test_call_tool("write_memory", {"content": "x"})


@pytest.mark.asyncio
async def test_two_tenants_one_store_cannot_see_each_other(pg):
    acme = create_mcp_server(pg, tenant_id=f"acme-{uuid.uuid4().hex[:6]}")
    globex = create_mcp_server(pg, tenant_id=f"globex-{uuid.uuid4().hex[:6]}")
    secret = f"ACME secret: Q4 bid is 4.2M EUR {uuid.uuid4().hex[:8]}"

    ref = (await _call(acme, "write_memory", {"content": secret}))["ref"]

    # The audit's exact leak: globex retrieves ACME's text word for word. Not any more.
    leak = await _call(globex, "retrieve_memories", {"query": "ACME Q4 bid"})
    assert all(secret not in m["content"] for m in leak["memories"])
    assert all(m["ref"] != ref for m in leak["memories"])

    # Non-vacuity: the OWNER still sees it through the same code path.
    own = await _call(acme, "retrieve_memories", {"query": "ACME Q4 bid"})
    assert any(m["ref"] == ref for m in own["memories"])

    # list, delete and verify are scoped the same way: a foreign ref is NOT FOUND.
    assert all(m["ref"] != ref for m in (await _call(globex, "list_memories"))["memories"])
    assert any(m["ref"] == ref for m in (await _call(acme, "list_memories"))["memories"])
    assert "not found" in (await _call(globex, "delete_memory", {"ref": ref}))["error"]
    v = await _call(globex, "verify_erasure", {"ref": ref, "reverify": True, "probe_query": "ACME"})
    assert v["storage_check"]["row_present"] is False    # not present *to globex*
    # ... and the fact is still there for its owner (the foreign delete did nothing).
    assert (await _call(acme, "verify_erasure", {"ref": ref}))["storage_check"]["row_present"] is True


@pytest.mark.asyncio
async def test_caller_supplied_tenant_argument_cannot_rebind(sqlite_path):
    server = create_mcp_server(_sqlite_factory(sqlite_path), tenant_id="bound")
    ref = (await _call(server, "write_memory", {"content": "x"}))["ref"]
    r = await _call(server, "verify_erasure", {"ref": ref, "tenant": "someone-else"})
    assert r["error"] == "tenant_mismatch"
    r = await _call(server, "delete_memory", {"ref": ref, "tenant": "someone-else"})
    assert r["error"] == "tenant_mismatch"
    # the same tenant name is fine
    assert (await _call(server, "verify_erasure", {"ref": ref, "tenant": "bound"}))["tenant"] == "bound"


# ── findings 10–13: conformance on a scratch store, engine verdicts, real evidence ─

@pytest.mark.asyncio
async def test_run_conformance_never_touches_the_serving_store(sqlite_path, tmp_path):
    server = create_mcp_server(_sqlite_factory(sqlite_path), tenant_id="t",
                               scratch_factory=_sqlite_factory(str(tmp_path / "scratch.db")))
    await _call(server, "write_memory", {"content": "real user memory"})
    # Count the WHOLE store, not the tenant-scoped list: the suite writes under its
    # own tenant ids, so a scoped count would hide the pollution (a vacuous test —
    # caught by flipping the scratch factory back to the live one).
    import sqlite3
    def rows():
        with sqlite3.connect(sqlite_path) as c:
            return c.execute("SELECT count(*) FROM memories").fetchone()[0]
    n0 = rows()

    r = await _call(server, "run_conformance", {"capability": "HARD_DELETE"})
    assert "error" not in r, r
    assert r["verdict"] in ("PASS", "VIOLATION")          # 0.4.2: 'no attribute metrics'
    assert r["declared"] is True
    assert r["directions"] and all({"name", "point", "passed"} <= set(d) for d in r["directions"])

    n1 = rows()
    assert n1 == n0 == 1                                  # 0.4.2: 1 → ~1172
    got = await _call(server, "retrieve_memories", {"query": "user memory"})
    assert all("Karnesh" not in m["content"] for m in got["memories"])

    # evidence is evidence: a digest over the results, not of the seed; store named
    import hashlib
    assert r["evidence"]["rollup_hash"] != hashlib.blake2b(b"1729").hexdigest()
    assert len(r["evidence"]["rollup_hash"]) == 64
    assert r["evidence"]["store"].startswith("scratch:")


@pytest.mark.asyncio
async def test_run_conformance_undeclared_is_not_declared_never_pass(sqlite_path, tmp_path):
    server = create_mcp_server(_sqlite_factory(sqlite_path), tenant_id="t",
                               scratch_factory=_sqlite_factory(str(tmp_path / "scratch.db")))
    r = await _call(server, "run_conformance", {"capability": "CONCURRENCY_CONTROL"})
    assert r["verdict"] == "NOT_DECLARED"                 # 0.4.2: PASS, enforced=True
    assert r["declared"] is False and r["enforced"] is False
    assert r["claim_matches_behavior"] is None


@pytest.mark.asyncio
async def test_run_conformance_without_scratch_store_refuses(sqlite_path):
    server = create_mcp_server(_sqlite_factory(sqlite_path), tenant_id="t")  # no scratch
    await _call(server, "write_memory", {"content": "real user memory"})
    r = await _call(server, "run_conformance", {"capability": "HARD_DELETE"})
    assert "error" in r and "scratch" in r["error"]
    assert (await _call(server, "list_memories"))["count"] == 1


# ── finding 14: verify errors are errors, not UNTESTED ──────────────────────

@pytest.mark.asyncio
async def test_get_capabilities_verify_uses_engine_and_never_swallows(sqlite_path, tmp_path):
    server = create_mcp_server(_sqlite_factory(sqlite_path), tenant_id="t",
                               scratch_factory=_sqlite_factory(str(tmp_path / "scratch.db")))
    r = await _call(server, "get_capabilities", {"verify": True, "capabilities": ["HARD_DELETE"]})
    assert r["capabilities"][0]["verdict"] in ("PASS", "VIOLATION")   # 0.4.2: UNTESTED for all

    def boom():
        raise RuntimeError("scratch exploded")
    broken = create_mcp_server(_sqlite_factory(sqlite_path), tenant_id="t", scratch_factory=boom)
    r = await _call(broken, "get_capabilities", {"verify": True, "capabilities": ["HARD_DELETE"]})
    assert r["capabilities"][0]["verdict"].startswith("ERROR: RuntimeError: scratch exploded")


# ── finding 4: stdout is the protocol channel ────────────────────────────────

def test_stdio_mode_writes_nothing_to_stdout_before_the_protocol(tmp_path):
    env = dict(os.environ, GRAFOMEM_MCP_TENANT="t")
    proc = subprocess.Popen(
        [sys.executable, "-m", "aml.cli", "serve", "--mcp", "stdio", "-b", "sqlite",
         "--db", str(tmp_path / "x.db"), "-e", "stub"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
    )
    try:
        time.sleep(3)  # let it start and print its banner
    finally:
        proc.kill()
        out, err = proc.communicate(timeout=10)
    assert out == b"", out                                # 0.4.2: banner on stdout
    assert b"GRAFOMEM MCP server (transport=stdio)" in err


def test_cli_requires_a_tenant_for_a_multi_tenant_backend(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "GRAFOMEM_MCP_TENANT"}
    r = subprocess.run(
        [sys.executable, "-m", "aml.cli", "serve", "--mcp", "stdio", "-b", "sqlite",
         "--db", str(tmp_path / "x.db"), "-e", "stub"],
        capture_output=True, env=env, timeout=60,
    )
    assert r.returncode != 0
    assert b"--tenant" in r.stderr and b"MULTI_TENANT" in r.stderr
