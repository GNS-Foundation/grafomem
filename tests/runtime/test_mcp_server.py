import pytest
import os, tempfile, shutil, json, base64
import numpy as np
from cryptography.hazmat.primitives.asymmetric import ed25519
from grafomem.cso import CSO
from grafomem.runtime import Registry
from grafomem.server import create_server
from grafomem.mcp import from_mcp_resource, MCP_CONTENT_TYPE

def _cso(d, capabilities, model_id, seed, expires_at=None, policy="tenant"):
    M = np.random.default_rng(seed).standard_normal((d, d)).astype(np.float32)
    c = {"subject_id": "did:x", "policy": policy}
    if expires_at: c["expires_at"] = expires_at
    return CSO(M=M, model_id=model_id, capabilities=frozenset(capabilities), consent=c, key_id="server-key")

import asyncio

def test_mcp_server_flow():
    asyncio.run(_test_mcp_server_flow_async())

async def _test_mcp_server_flow_async():
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key()
    trusted = {"server-key": pub}
    
    d, MODEL = 64, "grafomem-v0"
    temp_dir = tempfile.mkdtemp()
    
    try:
        reg = Registry(directory=temp_dir)
        c1 = _cso(d, ["medical.Diagnosis", "radiology.Read"], MODEL, 1)
        reg.register(c1, save=True, private_key=priv)
        
        # Instantiate server
        app = create_server(temp_dir)
        
        # Client: list resources
        resources = await app.list_resources()
        assert len(resources) == 1
        res = resources[0]
        assert str(res.uri).startswith("grafomem://cso/")
        
        # Check descriptor metadata
        desc = json.loads(res.description)
        assert desc["model_id"] == MODEL
        assert "medical.Diagnosis" in desc["capabilities"]
        
        # Client: read resource
        contents = await app.read_resource(str(res.uri))
        assert len(contents) == 1
        
        # Reconstruct the dict expected by from_mcp_resource
        mcp_payload = json.loads(contents[0].content)
        assert mcp_payload["contentType"] == MCP_CONTENT_TYPE
        mcp_payload["bytes"] = base64.b64decode(mcp_payload["bytes"])
        
        # Client: pass to from_mcp_resource using its own trust store
        loaded_cso = from_mcp_resource(mcp_payload, trusted)
        
        # Ensure it loaded properly
        assert loaded_cso.capabilities == frozenset(["medical.Diagnosis", "radiology.Read"])
        assert loaded_cso.model_id == MODEL
        assert np.allclose(loaded_cso.M, c1.M)
        
        # Tamper Test
        tampered_payload = mcp_payload.copy()
        gfm_bytes = bytearray(tampered_payload["bytes"])
        # flip a byte in the tensor section (offset past header)
        gfm_bytes[-10] ^= 0xFF
        tampered_payload["bytes"] = bytes(gfm_bytes)
        
        try:
            from_mcp_resource(tampered_payload, trusted)
            assert False, "Tampered payload should have failed"
        except ValueError as e:
            assert "signature mismatch" in str(e)
            
    finally:
        shutil.rmtree(temp_dir)
