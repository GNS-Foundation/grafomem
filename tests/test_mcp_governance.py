import pytest
import json
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
import httpx

from aml.server.mcp import create_mcp_server
from aml.backends.interface import Capability, WriteOptions

class DummyMemoryRecord:
    def __init__(self, ref, content, metadata=None):
        self.ref = ref
        self.content = content
        self.metadata = metadata or {}
        self.written_at = datetime.now(timezone.utc)

class MockBackend:
    def __init__(self, caps, behavior="honest", overrides=None):
        self.caps = caps
        self.behavior = behavior # honest, soft_delete, leaky_tenant, over_isolating
        self.store = {}
        self.db_url = "postgresql://mock"
        self.signing_identity = "mock-ed25519"
        self.overrides = overrides or {}

    def capabilities(self):
        return self.caps

    def get(self, ref):
        if self.behavior == "soft_delete" and ref in self.store:
            # Metadata might be removed or marked deleted, let's say it's None to simulate metadata check failing
            if self.store[ref].get("deleted"):
                return None
        if ref in self.store and not self.store[ref].get("deleted"):
            return DummyMemoryRecord(ref, self.store[ref]["content"], self.store[ref]["metadata"])
        return None

    def delete(self, ref):
        if ref in self.store:
            if self.behavior == "soft_delete":
                self.store[ref]["deleted"] = True
            else:
                del self.store[ref]
            return True
        return False

    def retrieve(self, query, opts):
        # Handle retrieving deleted records based on behavior
        res = []
        for ref, item in self.store.items():
            if item.get("deleted") and self.behavior == "soft_delete":
                res.append(DummyMemoryRecord(ref, item["content"], item["metadata"]))
                continue
            if not item.get("deleted"):
                res.append(DummyMemoryRecord(ref, item["content"], item["metadata"]))
        return res

    def write(self, content, opts):
        ref = len(self.store) + 1
        self.store[ref] = {"content": content, "metadata": opts.metadata, "deleted": False}
        return ref

    def flush(self):
        pass

@pytest.fixture
def mcp_server_honest():
    def factory():
        return MockBackend([Capability.HARD_DELETE, Capability.MULTI_TENANT], behavior="honest")
    return create_mcp_server(factory, tenant_id="t1")

@pytest.fixture
def mcp_server_soft_delete():
    def factory():
        return MockBackend([Capability.HARD_DELETE, Capability.MULTI_TENANT], behavior="soft_delete")
    return create_mcp_server(factory, tenant_id="t1")


# ── 0017: the MCP server is handed an erasure service explicitly ────────────
class _FakeEPS:
    """Stands in for ErasureProofService. `valid` is the in-process signature verdict;
    `verify_raises` simulates a verifier that cannot run (degraded)."""
    def __init__(self, valid=True, verify_raises=False):
        self.valid, self.verify_raises, self.cert, self.issued_with = valid, verify_raises, None, []

    def issue_certificate(self, **kw):
        self.issued_with.append(kw)
        c = MagicMock()
        c.certificate_id = "cert-123"; c.fact_content_hash = "abc"
        c.signature = b"sig"; c.public_key = b"pub"
        c.erasure_completed_at = datetime.now(timezone.utc)
        self.cert = c
        return c

    def get_by_fact(self, tenant_id, ref):
        return self.cert

    def verify_certificate(self, cert_id):
        if self.verify_raises:
            raise RuntimeError("verifier unavailable")
        return {"valid": self.valid, "certificate_id": cert_id}


def _server_with(eps, behavior="honest"):
    def factory():
        return MockBackend([Capability.HARD_DELETE, Capability.MULTI_TENANT], behavior=behavior)
    return create_mcp_server(factory, erasure_service=eps, tenant_id="t1")

@pytest.mark.asyncio
async def test_verify_erasure_honest_pass():
    eps = _FakeEPS(valid=True)
    server = _server_with(eps)
    res_w = await server._test_call_tool("write_memory", {"content": "secret fact"})
    ref = json.loads(res_w[0].text)["ref"]

    res_del = await server._test_call_tool("delete_memory", {"ref": ref})
    del_data = json.loads(res_del[0].text)
    assert del_data["sealed_probe"]["gone_from_retrieval"] is True
    assert del_data["certificate"]["cert_id"] == "cert-123"
    assert del_data["certificate_status"] == "issued"
    # 0016/0017: the certificate's content was observed BEFORE the delete.
    assert eps.issued_with[0]["fact_content"] == "secret fact"

    # 0016: a certificate alone is NOT a read-path verdict. No persisted probe, no
    # reverify → ERASED_UNVERIFIED, sealed_probe None; the signature check still ran
    # (in-process, 0017) and passed.
    res_v = await server._test_call_tool("verify_erasure", {"ref": ref})
    v_data = json.loads(res_v[0].text)
    assert v_data["status"] == "ERASED_UNVERIFIED"
    assert v_data["sealed_probe"] is None
    assert "no targeted probe on record" in v_data["warning"]
    assert v_data["tamper"] is False
    assert v_data["verify_url"] == "in-process"

    # ERASED_VERIFIED is EARNED by a real read-path probe (reverify + lure).
    res_v2 = await server._test_call_tool(
        "verify_erasure", {"ref": ref, "reverify": True, "probe_query": "secret fact"}
    )
    v2 = json.loads(res_v2[0].text)
    assert v2["status"] == "ERASED_VERIFIED"
    assert v2["fresh_probe"]["gone_from_retrieval"] is True
    assert v2["tamper"] is False

@pytest.mark.asyncio
async def test_verify_erasure_leak_caught(mcp_server_soft_delete):
    server = mcp_server_soft_delete
    res_w = await server._test_call_tool("write_memory", {"content": "secret fact"})
    ref = json.loads(res_w[0].text)["ref"]
    
    with patch("aml.cloud.erasure_proof.ErasureProofService") as MockEPS:
        mock_eps = MockEPS.return_value
        mock_cert = MagicMock()
        mock_cert.certificate_id = "cert-123"
        mock_cert.fact_content_hash = "abc"
        mock_cert.signature = b"sig"
        mock_cert.public_key = b"pub"
        mock_cert.signing_key_id = "key"
        mock_cert.erasure_completed_at = datetime.now(timezone.utc)
        mock_eps.issue_certificate.return_value = mock_cert
        mock_eps.get_by_fact.return_value = mock_cert
        
        # Delete it -> will leak
        res_del = await server._test_call_tool("delete_memory", {"ref": ref})
        del_data = json.loads(res_del[0].text)
        assert del_data["sealed_probe"]["gone_from_retrieval"] is False
        
        with patch("httpx.Client.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"verified": True}
            mock_get.return_value = mock_resp
            
            # Since sealed_probe logic isn't fully mocked correctly in my snippet, let's force reverify to catch the leak
            res_v = await server._test_call_tool("verify_erasure", {"ref": ref, "reverify": True, "probe_query": "secret"})
            v_data = json.loads(res_v[0].text)
            
            assert v_data["status"] == "NOT_ERASED"

@pytest.mark.asyncio
async def test_verify_erasure_degraded_verifier():
    # The in-process verifier cannot run → tamper is None (unknown), never False.
    server = _server_with(_FakeEPS(valid=True, verify_raises=True))
    res_w = await server._test_call_tool("write_memory", {"content": "secret fact"})
    ref = json.loads(res_w[0].text)["ref"]
    await server._test_call_tool("delete_memory", {"ref": ref})

    res_v = await server._test_call_tool(
        "verify_erasure", {"ref": ref, "reverify": True, "probe_query": "secret"}
    )
    v_data = json.loads(res_v[0].text)
    assert v_data["status"] == "ERASED_UNVERIFIED"
    assert v_data["tamper"] is None
    assert "unreachable" in v_data["warning"]

@pytest.mark.asyncio
async def test_verify_erasure_no_targeted_lure():
    server = _server_with(_FakeEPS(valid=True))
    res_w = await server._test_call_tool("write_memory", {"content": "secret fact"})
    ref = json.loads(res_w[0].text)["ref"]
    await server._test_call_tool("delete_memory", {"ref": ref})

    # reverify=True without probe_query. 0016: with no lure there is no read-path
    # probe, and the certificate's existence does NOT stand in for one.
    res_v = await server._test_call_tool("verify_erasure", {"ref": ref, "reverify": True})
    v_data = json.loads(res_v[0].text)
    assert v_data["status"] == "ERASED_UNVERIFIED"
    assert v_data["sealed_probe"] is None
    assert "returning sealed verdict only" in v_data["warning"]

@pytest.mark.asyncio
async def test_verify_erasure_tampered_cert():
    # The in-process signature check fails → tamper True, status stays UNVERIFIED.
    server = _server_with(_FakeEPS(valid=False))
    res_w = await server._test_call_tool("write_memory", {"content": "secret fact"})
    ref = json.loads(res_w[0].text)["ref"]
    await server._test_call_tool("delete_memory", {"ref": ref})

    res_v = await server._test_call_tool(
        "verify_erasure", {"ref": ref, "reverify": True, "probe_query": "secret fact"}
    )
    v_data = json.loads(res_v[0].text)
    assert v_data["status"] == "ERASED_UNVERIFIED"
    assert v_data["tamper"] is True
    assert "tamper" in v_data["warning"].lower()


@pytest.mark.asyncio
async def test_verify_erasure_remote_check_can_only_worsen(monkeypatch):
    # Local verdict valid; an optional GRAFOMEM_VERIFY_URL that says "not verified"
    # flips tamper to True. The reverse (remote says fine, local says tampered) must
    # NOT rescue the certificate — covered by the tampered test above with no remote.
    monkeypatch.setenv("GRAFOMEM_VERIFY_URL", "https://verifier.example/v1/erasure/cert-123/verify")
    server = _server_with(_FakeEPS(valid=True))
    res_w = await server._test_call_tool("write_memory", {"content": "secret fact"})
    ref = json.loads(res_w[0].text)["ref"]
    await server._test_call_tool("delete_memory", {"ref": ref})

    with patch("httpx.Client.get") as mock_get:
        mock_resp = MagicMock(); mock_resp.status_code = 200
        mock_resp.json.return_value = {"verified": False}
        mock_get.return_value = mock_resp
        res_v = await server._test_call_tool(
            "verify_erasure", {"ref": ref, "reverify": True, "probe_query": "secret fact"}
        )
    v_data = json.loads(res_v[0].text)
    assert v_data["tamper"] is True
    assert v_data["verify_url"].startswith("https://verifier.example")

@pytest.mark.asyncio
async def test_verify_erasure_storage_neq_erasure(mcp_server_soft_delete):
    # Tests that row_present could be false (it's soft deleted) but semantic probe returns it
    server = mcp_server_soft_delete
    res_w = await server._test_call_tool("write_memory", {"content": "secret fact"})
    ref = json.loads(res_w[0].text)["ref"]
    
    with patch("aml.cloud.erasure_proof.ErasureProofService") as MockEPS:
        mock_eps = MockEPS.return_value
        mock_cert = MagicMock()
        mock_cert.certificate_id = "cert-123"
        mock_cert.fact_content_hash = "abc"
        mock_cert.signature = b"sig"
        mock_cert.public_key = b"pub"
        mock_cert.signing_key_id = "key"
        mock_cert.erasure_completed_at = datetime.now(timezone.utc)
        mock_eps.issue_certificate.return_value = mock_cert
        # Mock that we don't have a valid cert
        mock_eps.get_by_fact.return_value = None
        
        await server._test_call_tool("delete_memory", {"ref": ref})
        
        # Soft delete means get(ref) is None, but retrieve() might still find it.
        # storage_check.row_present should be False.
        res_v = await server._test_call_tool("verify_erasure", {"ref": ref, "reverify": True, "probe_query": "secret fact"})
        v_data = json.loads(res_v[0].text)
        
        assert v_data["storage_check"]["row_present"] is False
        assert v_data["status"] == "NOT_ERASED"


# ── run_conformance through MCP, against the REAL engine (finding 11) ────────
# The previous tests here mocked run_conformance to return `.metrics`/`.value`
# objects — a shape the engine never had, which is how the crash shipped. These
# use the engine's own reference backends (the ones that prove the engine sound).

def _ref_server(cls, tenant=None):
    from aml.backends.vector_only import _stub_embedder
    batch = _stub_embedder()
    emb = lambda t: batch([t])[0] if isinstance(t, str) else batch(t)
    factory = lambda: cls(embed_fn=emb)
    return create_mcp_server(factory, tenant_id=tenant, scratch_factory=factory)


@pytest.mark.asyncio
async def test_run_conformance_soft_delete_is_a_violation():
    from aml.backends.delete_backends import SoftDeleteBackend
    r = json.loads((await _ref_server(SoftDeleteBackend)._test_call_tool(
        "run_conformance", {"capability": "HARD_DELETE"}))[0].text)
    assert r["verdict"] == "VIOLATION" and r["enforced"] is False
    assert any(d["name"].startswith("deleted-fact leakage") and not d["passed"] for d in r["directions"])


@pytest.mark.asyncio
async def test_run_conformance_honest_delete_passes():
    from aml.backends.delete_backends import HonestDeleteBackend
    r = json.loads((await _ref_server(HonestDeleteBackend)._test_call_tool(
        "run_conformance", {"capability": "HARD_DELETE"}))[0].text)
    assert r["verdict"] == "PASS" and r["claim_matches_behavior"] is True


@pytest.mark.asyncio
async def test_run_conformance_leaky_tenant_is_a_violation():
    from aml.backends.tenant_backends import LeakyTenant
    r = json.loads((await _ref_server(LeakyTenant, tenant="t1")._test_call_tool(
        "run_conformance", {"capability": "MULTI_TENANT"}))[0].text)
    assert r["verdict"] == "VIOLATION"


@pytest.mark.asyncio
async def test_run_conformance_scoped_tenant_passes():
    from aml.backends.tenant_backends import TenantScoped
    r = json.loads((await _ref_server(TenantScoped, tenant="t1")._test_call_tool(
        "run_conformance", {"capability": "MULTI_TENANT"}))[0].text)
    assert r["verdict"] == "PASS"
