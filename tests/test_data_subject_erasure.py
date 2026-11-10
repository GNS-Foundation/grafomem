"""0019 — DATA_SUBJECT scope (GMP §7.7) and per-data-subject crypto-erasure in Cloud.

Three layers, each with its guard proven non-vacuous (a flip that must fail is written as
a test here, not left to memory):

  1. backends — SQLite (reference) and Postgres honour data_subject_id, erase two-sided,
     superseded versions included; non-claiming backends refuse the option (§7.7).
  2. key hierarchy — a retained ciphertext copy decrypts BEFORE the erasure and is
     unrecoverable AFTER; rows written before the opt-in stay tenant-keyed.
  3. cloud — the route erases facts + embeddings + key, one certificate per fact with the
     key fingerprint bound; verify checks the tombstone (a restored key FAILS verify);
     restore-scrub re-destroys a revived key from the ledger; MCP tool.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid

import pytest

from aml.backends.interface import Capability, CapabilityNotSupported, RetrieveOptions, WriteOptions
from aml.backends.vector_only import _stub_embedder

DB_URL = os.environ.get("GRAFOMEM_DB_URL")
LEDGER_URL = os.environ.get("GRAFOMEM_LEDGER_URL")


def _embed(dim):
    batch = _stub_embedder(dim)
    return lambda t: batch([t])[0] if isinstance(t, str) else batch(t)


def _sqlite(path):
    from aml.backends.sqlite_gmp import SQLiteGMPBackend
    return SQLiteGMPBackend(db_path=path, embed_fn=_embed(64))


def _pg():
    if not DB_URL:
        pytest.skip("GRAFOMEM_DB_URL not set")
    from aml.backends.postgres_gmp import PostgresGMPBackend
    return PostgresGMPBackend(DB_URL, embed_fn=_embed(384))


# ── 1. backends ──────────────────────────────────────────────────────────────

def _two_sided(store, tenant, supersede):
    o = lambda ds=None: WriteOptions(tenant_id=tenant, data_subject_id=ds)
    a1 = store.write("alice lives in rome", o("alice"))
    a2 = supersede(a1, "alice lives in milan", o("alice"))
    b1 = store.write("bob lives in rome", o("bob"))
    n1 = store.write("nobody in particular", o())
    store.flush()
    assert store.get(a2).data_subject_id == "alice" and store.get(n1).data_subject_id is None

    refs = store.erase_data_subject("alice", WriteOptions(tenant_id=tenant))
    store.flush()
    assert sorted(refs) == sorted([a1, a2])                       # superseded version too
    assert not store.exists(a1) and not store.exists(a2)
    assert store.exists(b1) and store.exists(n1)                   # no over-erasure
    audit = {m.ref for m in store.audit()}
    assert not ({a1, a2} & audit) and {b1, n1} <= audit
    got = {m.ref for m in store.retrieve("rome", RetrieveOptions(tenant_id=tenant, budget_tokens=4096))}
    assert not ({a1, a2} & got) and b1 in got
    assert store.erase_data_subject("alice", WriteOptions(tenant_id=tenant)) == []   # idempotent
    assert store.erase_data_subject("bob", WriteOptions(tenant_id="someone-else")) == []  # tenant-bounded
    assert store.exists(b1)
    return b1, n1


def test_sqlite_reference_backend_is_two_sided(tmp_path):
    s = _sqlite(str(tmp_path / "x.db"))
    assert Capability.DATA_SUBJECT in s.capabilities()
    _two_sided(s, "t", lambda ref, c, o: s.supersede(ref, c, o))


def test_postgres_backend_is_two_sided_and_deletes_embeddings_synchronously():
    import psycopg
    s = _pg()
    assert Capability.DATA_SUBJECT in s.capabilities()
    t = "ds-" + uuid.uuid4().hex[:6]
    try:
        o = WriteOptions(tenant_id=t, data_subject_id="carol")
        c1 = s.write("carol one", o)
        with psycopg.connect(DB_URL) as conn:
            assert conn.execute("SELECT count(*) FROM memory_embeddings WHERE ref = %s", (c1,)).fetchone()[0] == 1
        b1, n1 = _two_sided(s, t, lambda ref, c, o: s.supersede(ref, c, None, o))
        s.erase_data_subject("carol", WriteOptions(tenant_id=t))
        with psycopg.connect(DB_URL) as conn:
            # F3: gone now, not after the sweeper window (0.5.0 marks erasure_pending instead)
            assert conn.execute("SELECT count(*) FROM memory_embeddings WHERE ref = %s", (c1,)).fetchone()[0] == 0
        for r in (b1, n1):
            s.delete(r)
    finally:
        s._pool.close()


def test_non_claiming_backends_refuse_the_option(tmp_path):
    from aml.backends.gmp_reference import GMPReferenceBackend
    try:
        from aml.backends.hnsw_gmp import HnswGMPBackend
        h = HnswGMPBackend(db_path=str(tmp_path / "h.db"), embed_fn=_stub_embedder(64))
    except RuntimeError:
        h = None   # hnswlib not installed here; the guard is the same two lines as the reference backend's
    if h is not None:
        assert Capability.DATA_SUBJECT not in h.capabilities()
        with pytest.raises(CapabilityNotSupported):
            h.write("x", WriteOptions(data_subject_id="alice"))
        assert isinstance(h.write("x", WriteOptions()), int)   # unscoped writes still fine
    g = GMPReferenceBackend(embed_fn=_stub_embedder(64))
    with pytest.raises(CapabilityNotSupported):
        g.write("x", WriteOptions(data_subject_id="alice"))


def test_conformance_row_is_two_directional_and_not_vacuous(tmp_path):
    from aml.eval.conformance import _test_data_subject
    from aml.backends.sqlite_gmp import SQLiteGMPBackend
    n = [0]
    def mk(cls):
        def f():
            n[0] += 1
            return cls(db_path=str(tmp_path / f"{cls.__name__}{n[0]}.db"), embed_fn=_embed(64))
        return f
    assert _test_data_subject(mk(SQLiteGMPBackend), [1, 2], None).passed

    class NoOp(SQLiteGMPBackend):
        def erase_data_subject(self, d, o): return []
    class EraseAll(SQLiteGMPBackend):
        def erase_data_subject(self, d, o):
            refs = [m.ref for m in self.audit()]
            for r in refs: self.delete(r)
            return refs
    r = _test_data_subject(mk(NoOp), [1, 2], None)
    assert not r.passed and not r.directions[0].passed          # leak direction fails
    r = _test_data_subject(mk(EraseAll), [1, 2], None)
    assert not r.passed and not r.directions[1].passed          # over-erasure direction fails


# ── 2. key hierarchy ─────────────────────────────────────────────────────────

@pytest.fixture
def tkm():
    if not LEDGER_URL:
        pytest.skip("GRAFOMEM_LEDGER_URL not set")
    from aml.cloud.tenant_key_manager import TenantKeyManager
    k = TenantKeyManager("a" * 64, LEDGER_URL)
    k.ensure_schema()
    k.ensure_data_subject_schema()
    yield k
    k._pool.close()


def test_retained_ciphertext_dies_with_the_key(tkm):
    import psycopg
    from aml.cloud.tenant_key_manager import DataSubjectKeyDestroyed, DS_PREFIX
    opted = set()
    tkm.configure_data_subject_keys(lambda t: t in opted)
    s = _pg(); s._encryption = tkm
    t = "ds-" + uuid.uuid4().hex[:6]
    try:
        old = s.write("alice before opt-in", WriteOptions(tenant_id=t, data_subject_id="alice"))
        opted.add(t)
        a1 = s.write("alice lives in rome", WriteOptions(tenant_id=t, data_subject_id="alice"))
        b1 = s.write("bob lives in rome", WriteOptions(tenant_id=t, data_subject_id="bob"))
        with psycopg.connect(DB_URL) as c:
            enc = dict(c.execute("SELECT ref, content_enc FROM memories WHERE ref = ANY(%s)", ([old, a1, b1],)).fetchall())
        assert enc[a1].startswith(DS_PREFIX) and enc[b1].startswith(DS_PREFIX)
        assert not enc[old].startswith(DS_PREFIX)                 # F7: pre-opt-in row is tenant-keyed
        assert [s.get(r).content for r in (old, a1, b1)] == ["alice before opt-in", "alice lives in rome", "bob lives in rome"]
        assert s.row_is_data_subject_keyed(a1) and not s.row_is_data_subject_keyed(old)

        retained = enc[a1]   # stands in for WAL / dead tuple / backup residue
        assert tkm.get_data_subject_encryptor(t, "alice", require=True).decrypt(retained) == "alice lives in rome"

        s.erase_data_subject("alice", WriteOptions(tenant_id=t))
        fp = tkm.destroy_data_subject_key(t, "alice")
        assert fp and tkm.data_subject_key_status(t, "alice") == ("destroyed", fp)
        tkm._ds_cache.clear()
        with pytest.raises(DataSubjectKeyDestroyed):
            tkm.get_data_subject_encryptor(t, "alice", require=True).decrypt(retained)
        assert s.get(b1).content == "bob lives in rome"            # bob's key untouched
        assert tkm.destroy_data_subject_key(t, "alice") == fp      # idempotent
        assert tkm.destroy_data_subject_key(t, "never-keyed") is None
        s.delete(b1)
    finally:
        s._pool.close()


def test_key_is_random_not_derived_and_hierarchy_holds(tkm):
    """Two subjects of one tenant get different keys; a different tenant cannot unwrap
    another tenant's subject key (wrapped under THAT tenant's DEK)."""
    tkm.configure_data_subject_keys(lambda t: True)
    t1, t2 = "k-" + uuid.uuid4().hex[:6], "k-" + uuid.uuid4().hex[:6]
    e_a = tkm.get_data_subject_encryptor(t1, "alice")
    e_b = tkm.get_data_subject_encryptor(t1, "bob")
    assert e_a.fingerprint != e_b.fingerprint
    assert e_a.decrypt(e_a.encrypt("x")) == "x"
    with pytest.raises(ValueError):
        e_b.decrypt(e_a.encrypt("x"))
    # hierarchy: destroying the TENANT key makes alice's wrapped DEK unrecoverable too
    tkm.destroy_tenant_key(t1)
    tkm._ds_cache.clear()
    from aml.cloud.tenant_key_manager import TenantKeyDestroyed
    with pytest.raises(TenantKeyDestroyed):
        tkm.get_data_subject_encryptor(t1, "alice", require=True)


# ── 3. cloud: route, certificates, verify, restore-scrub, MCP ────────────────

@pytest.fixture(scope="module")
def _e2e():
    if not DB_URL:
        pytest.skip("GRAFOMEM_DB_URL not set")
    from fastapi.testclient import TestClient
    from aml.server.app import create_app
    from aml.cloud.tenant_manager import TenantManager
    os.environ["AUTH_MODE"] = "cloud"
    os.environ.setdefault("GRAFOMEM_SIGNING_KEY", "b" * 64)
    tm = TenantManager(DB_URL)
    tm.ensure_schema()
    info = tm.create_tenant(name=f"ds-{uuid.uuid4().hex[:8]}")
    # create_app's default memory factory is an in-memory SQLite store (the Cloud services
    # are on Postgres, the memories are not). Data-subject keys live in the Postgres
    # backend's write path, so this test runs the memories on Postgres — as `serve -b
    # postgres` does.
    from aml.backends.postgres_gmp import PostgresGMPBackend
    app = create_app(db_url=DB_URL, backend_factory=lambda: PostgresGMPBackend(DB_URL, embed_fn=_embed(384)))
    with TestClient(app) as client:
        yield {"client": client, "key": info.api_key, "tenant_id": info.id, "app": app}


def test_route_erases_subject_certifies_each_fact_and_verify_checks_the_tombstone(_e2e):
    import psycopg
    from psycopg.rows import dict_row
    c, h, tid = _e2e["client"], {"Authorization": f"Bearer {_e2e['key']}"}, _e2e["tenant_id"]
    store = c.post("/v1/stores", headers=h).json()["store_id"]
    w = lambda content, ds=None: c.post(f"/v1/stores/{store}/write", headers=h,
                                        json={"content": content, "options": {"data_subject_id": ds}}).json()["ref"]
    old = w("alice before opt-in", "alice")                         # tenant-keyed (F7)
    r = c.post("/v1/erasure/data-subject-encryption", headers=h, json={"enabled": True})
    assert r.status_code == 200 and r.json()["data_subject_encryption"] is True
    a1, a2 = w("alice lives in rome", "alice"), w("alice likes espresso", "alice")
    b1, n1 = w("bob lives in rome", "bob"), w("nobody")
    assert c.get(f"/v1/stores/{store}/audit", headers=h).status_code == 200

    r = c.post("/v1/erasure/data-subject", headers=h, json={"data_subject_id": "alice"})
    assert r.status_code == 200, r.text
    res = r.json()
    assert sorted(res["fact_refs"]) == sorted([old, a1, a2]) and res["facts"] == 3
    assert res["key_destroyed"] is True and res["key_fingerprint"]
    assert res["keyed_facts"] == 2                                   # old row was never key-covered
    assert len(res["certificates"]) == 3 and res["certificate_failures"] == []
    assert res["ledger_entry_id"]
    assert "alice" not in json.dumps(res)                           # the id never leaves as plaintext
    fp = res["key_fingerprint"]

    with psycopg.connect(DB_URL, row_factory=dict_row) as conn:
        certs = {row["fact_ref"]: row for row in conn.execute(
            "SELECT fact_ref, coverage, governance_record FROM erasure_certificates WHERE certificate_id = ANY(%s)",
            (res["certificates"],))}
        emb_left = conn.execute("SELECT count(*) FROM memory_embeddings WHERE ref = ANY(%s)", ([old, a1, a2],)).fetchone()["count"]
    assert emb_left == 0                                            # F3 synchronous
    for ref in (a1, a2):
        assert certs[ref]["coverage"]["primary"] == "absent"
        assert certs[ref]["coverage"]["data_subject_key"] == "absent"
        assert certs[ref]["governance_record"]["addressing"]["data_subject_key_fingerprint"] == fp
    assert certs[old]["coverage"]["data_subject_key"] == "unverified"   # honest: deletion only
    assert "data_subject_key_fingerprint" not in certs[old]["governance_record"]["addressing"]

    # survivors
    assert c.post(f"/v1/stores/{store}/retrieve", headers=h, json={"query": "rome", "options": {}}).status_code == 200
    got = {m["ref"] for m in c.post(f"/v1/stores/{store}/retrieve", headers=h,
                                    json={"query": "lives in rome", "options": {}}).json()["memories"]}
    assert b1 in got and not ({a1, a2, old} & got)

    # verify: signature AND tombstone
    cert_id = next(cid for cid in res["certificates"]
                   if c.get(f"/v1/erasure/{cid}", headers=h).json()["fact_ref"] == a1)
    v = c.get(f"/v1/erasure/{cert_id}/verify", headers=h).json()
    assert v["valid"] is True and v["data_subject_key"]["ok"] is True, v

    # NON-VACUITY (F5): revive the key as a restore would → the same certificate FAILS verify
    with psycopg.connect(LEDGER_URL) as conn:
        conn.execute("UPDATE data_subject_deks SET destroyed_at = NULL, wrapped_dek = 'REVIVED' "
                     "WHERE tenant_id = %s AND key_fingerprint = %s", (tid, fp))
    v = c.get(f"/v1/erasure/{cert_id}/verify", headers=h).json()
    assert v["valid"] is False and "ACTIVE" in v["detail"], v

    # restore-scrub replays the ledger and re-destroys it; verify passes again
    from aml.cloud.restore_scrub import reapply_ledger
    st = _e2e["app"].state
    stats = reapply_ledger(st.erasure_ledger, st.store_manager, st.tenant_key_manager, st.decision_trail)
    assert stats["data_subject_keys_redestroyed"] >= 1, stats
    v = c.get(f"/v1/erasure/{cert_id}/verify", headers=h).json()
    assert v["valid"] is True, v

    # idempotent route; ledger chain intact
    assert c.post("/v1/erasure/data-subject", headers=h, json={"data_subject_id": "alice"}).json()["facts"] == 0
    lv = c.get("/v1/erasure/ledger/verify", headers=h).json()
    assert lv["ok"] is True, lv
    for ref in (b1, n1):
        c.post(f"/v1/stores/{store}/delete", headers=h, json={"ref": ref})


def test_verify_fails_when_the_claim_cannot_be_checked():
    """A certificate claiming data_subject_key=absent with no key manager attached is
    NOT valid: an uncheckable claim is not a verified claim."""
    from aml.cloud.erasure_proof import ErasureProofService
    class _Cert:
        coverage = {"data_subject_key": "absent"}
        governance_record = {"addressing": {"data_subject_key_fingerprint": "f" * 32}}
        tenant_id = "t"
    svc = ErasureProofService.__new__(ErasureProofService)
    r = svc._verify_data_subject_key_claim(_Cert())
    assert r["ok"] is False and "no key manager" in r["detail"]
    _Cert.governance_record = {"addressing": {}}
    r = svc._verify_data_subject_key_claim(_Cert())
    assert r["ok"] is False and "no key fingerprint" in r["detail"]
    _Cert.coverage = {"primary": "absent"}
    assert svc._verify_data_subject_key_claim(_Cert()) is None      # no claim → nothing to check


@pytest.mark.asyncio
async def test_mcp_erase_data_subject_without_service_is_honest(tmp_path):
    from aml.server.mcp import create_mcp_server
    server = create_mcp_server(lambda: _sqlite(str(tmp_path / "m.db")), tenant_id="t")
    async def call(tool, args):
        return json.loads((await server._test_call_tool(tool, args))[0].text)
    a = (await call("write_memory", {"content": "alice", "data_subject_id": "alice"}))["ref"]
    b = (await call("write_memory", {"content": "bob", "data_subject_id": "bob"}))["ref"]
    r = await call("erase_data_subject", {"data_subject_id": "alice"})
    assert r["fact_refs"] == [a] and r["key_destroyed"] is False
    assert r["certificate_status"].startswith("not_issued")
    assert (await call("verify_erasure", {"ref": a, "reverify": True}))["storage_check"]["row_present"] is False
    assert (await call("list_memories", {}))["count"] == 1
    assert (await call("erase_data_subject", {"data_subject_id": "bob", "tenant": "other"}))["error"] == "tenant_mismatch"
