"""Conformance + lifecycle tests (SPEC-1.0 §7). Runs under pytest OR as a script."""
import numpy as np
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives.asymmetric import ed25519
from grafomem import (CSO, Registry, Selector, Scheduler, Linker, Governance,
                   ExecutionContext, Loader, to_mcp_resource, from_mcp_resource, InfeasibleSchedule)

# Shared keys for tests
_priv = ed25519.Ed25519PrivateKey.generate()
_pub = _priv.public_key()
_trusted = {"key1": _pub}

def _cso(d, caps, model_id, seed, policy="tenant", expires_at=None, version="1.0.0", key_id="key1"):
    g = np.random.default_rng(seed)
    K = g.standard_normal((8, d)); K /= np.linalg.norm(K, axis=1, keepdims=True)
    V = g.standard_normal((8, d)); V /= np.linalg.norm(V, axis=1, keepdims=True)
    M = sum(np.outer(V[i], K[i]) for i in range(8)) * 0.1
    return CSO(M=M.astype(np.float32), model_id=model_id, capabilities=frozenset(caps),
               consent={"subject_id": "did:x", "policy": policy, "expires_at": expires_at},
               meta={"version": version}, key_id=key_id)

def test_gfm_roundtrip_and_signature():
    c = _cso(64, ["lang.OldNorseTranslation"], "grafomem-v0", 1)
    c2 = CSO.from_gfm(c.to_gfm(_priv), _trusted)
    assert np.allclose(c.M, c2.M) and c.capabilities == c2.capabilities and c2.consent["policy"] == "tenant"
    
    # Tamper signature
    b = bytearray(c.to_gfm(_priv)); b[-5] ^= 0x7
    try: CSO.from_gfm(bytes(b), _trusted); assert False, "should reject"
    except ValueError as e: assert "signature mismatch" in str(e)
    
    # Tamper tensor
    b = bytearray(c.to_gfm(_priv)); b[100] ^= 0x1
    try: CSO.from_gfm(bytes(b), _trusted); assert False, "should reject"
    except ValueError as e: assert "signature mismatch" in str(e)

def test_codec_fuzzing():
    c = _cso(64, ["lang.OldNorseTranslation"], "grafomem-v0", 1)
    valid_b = c.to_gfm(_priv)
    
    # wrong magic
    b = bytearray(valid_b); b[0] = ord('X')
    try: CSO.from_gfm(bytes(b), _trusted); assert False
    except ValueError as e: assert "bad magic" in str(e)
    
    # truncated buffer (overall)
    try: CSO.from_gfm(valid_b[:-1], _trusted); assert False
    except ValueError: pass
    
    # corrupted header length prefix (make it huge)
    import struct
    b = bytearray(valid_b)
    b[4:8] = struct.pack("<I", 9999999)
    try: CSO.from_gfm(bytes(b), _trusted); assert False
    except ValueError as e: assert "truncated buffer" in str(e)

    # invalid json header
    # we'll inject bad json bytes
    # the header starts at offset 8, length hl
    hl = struct.unpack("<I", valid_b[4:8])[0]
    b = bytearray(valid_b)
    b = bytearray(valid_b)
    b[8] = ord('{')
    b[9] = ord('!') # invalid json
    try: CSO.from_gfm(bytes(b), _trusted); assert False
    except ValueError as e: assert "signature mismatch" in str(e) or "malformed header" in str(e)

def test_header_validation():
    # Expired consent
    exp = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    c = _cso(64, ["lang.OldNorseTranslation"], "grafomem-v0", 1, expires_at=exp)
    try: CSO.from_gfm(c.to_gfm(_priv), _trusted); assert False
    except ValueError as e: assert "Consent expired" in str(e)

    # Invalid policy
    c = _cso(64, ["lang.OldNorseTranslation"], "grafomem-v0", 1, policy="unknown")
    try: CSO.from_gfm(c.to_gfm(_priv), _trusted); assert False
    except ValueError as e: assert "Invalid consent policy" in str(e)
    
    # Invalid semver
    c = _cso(64, ["lang.OldNorseTranslation"], "grafomem-v0", 1, version="1.0")
    try: CSO.from_gfm(c.to_gfm(_priv), _trusted); assert False
    except ValueError as e: assert "Invalid meta.version semver" in str(e)
    
    # Invalid capability format
    c = _cso(64, ["invalid_cap"], "grafomem-v0", 1)
    try: CSO.from_gfm(c.to_gfm(_priv), _trusted); assert False
    except ValueError as e: assert "Invalid capability format" in str(e)

def test_lifecycle():
    d, MODEL = 64, "grafomem-v0"; reg = Registry()
    reg.register(_cso(d, ["medical.Diagnosis"], MODEL, 1))
    reg.register(_cso(d, ["radiology.Read", "medical.Diagnosis"], MODEL, 2))
    reg.register(_cso(d, ["radiology.Read"], "other-model", 5))  # wrong ABI
    gov = Governance(MODEL, norm_budget=2.0); sel = Selector(reg); sched = Scheduler(gov)
    required = ["medical.Diagnosis", "radiology.Read"]
    cands = sel.by_capability(required, model_id=MODEL)
    chosen = sched.schedule(cands, required)
    ctx = Linker.link(ExecutionContext(model_id=MODEL), chosen, gov)
    assert np.linalg.norm(ctx.M) <= gov.norm_budget
    assert required[0] in set().union(*[c.capabilities for c in chosen])
    assert ctx.run(np.random.default_rng(0).standard_normal(d)).shape == (d,)

def test_registry_and_conformance():
    import tempfile, shutil
    d, MODEL = 64, "grafomem-v0"
    temp_dir = tempfile.mkdtemp()
    try:
        reg1 = Registry(directory=temp_dir)
        c1 = _cso(d, ["medical.Diagnosis", "radiology.Read"], MODEL, 1)
        c2 = _cso(d, ["nephrology.Read"], MODEL, 2)
        reg1.register(c1, save=True, private_key=_priv)
        reg1.register(c2, save=True, private_key=_priv)
        
        # Conformance check
        vocab = {"medical.Diagnosis", "radiology.Read", "nephrology.Read"}
        reg1.check_conformance(vocab)  # Should pass
        
        try:
            reg1.check_conformance({"medical.Diagnosis"})
            assert False, "should fail on unknown capabilities"
        except ValueError as e:
            assert "non-conformant" in str(e)

        # Persistent load
        reg2 = Registry(directory=temp_dir, trusted_keys=_trusted)
        assert len(reg2._by_id) == 2
        # Verify indexes
        assert len(reg2.find(capability="medical.Diagnosis")) == 1
        assert len(reg2.find(model_id=MODEL)) == 2
        
        # Test selector subset and ABI matching
        sel = Selector(reg2)
        cands = sel.by_capability(["medical.Diagnosis", "radiology.Read"], model_id=MODEL)
        assert len(cands) == 1
        assert cands[0][1].model_id == MODEL
        
    finally:
        shutil.rmtree(temp_dir)

def test_registry_skip_and_record():
    import tempfile, shutil, os
    d, MODEL = 64, "grafomem-v0"
    temp_dir = tempfile.mkdtemp()
    try:
        reg1 = Registry(directory=temp_dir)
        c1 = _cso(d, ["medical.Diagnosis"], MODEL, 1)
        reg1.register(c1, save=True, private_key=_priv)
        
        # Write a bad file
        with open(os.path.join(temp_dir, "bad.gfm"), "wb") as f:
            f.write(b"this is definitely not a valid gfm file")
            
        # Load registry
        reg2 = Registry(directory=temp_dir, trusted_keys=_trusted)
        assert len(reg2._by_id) == 1
        assert len(reg2.load_errors) == 1
        assert reg2.load_errors[0][0] == "bad.gfm"
        assert "bad magic" in reg2.load_errors[0][1]
    finally:
        shutil.rmtree(temp_dir)

def test_registry_dedup_signers():
    d, MODEL = 64, "grafomem-v0"
    reg = Registry()
    
    # Same state (M and capabilities) under two different keys
    c1 = _cso(d, ["medical.Diagnosis"], MODEL, 1)
    c1.key_id = "alice"
    
    c2 = _cso(d, ["medical.Diagnosis"], MODEL, 1) # same seed, same M
    c2.key_id = "bob"
    
    assert c1.content_hash() == c2.content_hash()
    
    reg.register(c1)
    reg.register(c2)
    
    # Assert they dedup to one entry
    assert len(reg._by_id) == 1
    assert len(reg.find(capability="medical.Diagnosis")) == 1

def test_conformance_rules():
    d, MODEL = 64, "grafomem-v0"; gov = Governance(MODEL, norm_budget=0.05)  # tiny budget
    big = _cso(d, ["x.Y"], MODEL, 3)  # ‖M‖≈0.28 > 0.05 -> infeasible
    try: Linker.link(ExecutionContext(model_id=MODEL), [big], gov); assert False
    except RuntimeError: pass
    # behavioral routing is research
    try: Selector(Registry()).behavioral(None); assert False
    except NotImplementedError: pass
    # private consent rejected by default policy
    priv = _cso(d, ["x.Y"], MODEL, 4, policy="private")
    assert not gov.policy_ok(priv)

def test_set_cover_scheduling():
    d, MODEL = 64, "grafomem-v0"; reg = Registry()
    # CSO 1 has A, CSO 2 has B. Neither has both.
    reg.register(_cso(d, ["medical.Diagnosis"], MODEL, 1))
    reg.register(_cso(d, ["radiology.Read"], MODEL, 2))
    
    gov = Governance(MODEL, norm_budget=2.0); sel = Selector(reg); sched = Scheduler(gov)
    required = ["medical.Diagnosis", "radiology.Read"]
    cands = sel.by_capability(required, model_id=MODEL)
    
    # Both should be candidates because both are contributors
    assert len(cands) == 2
    
    chosen = sched.schedule(cands, required)
    # The scheduler should pick both to cover the requirement
    assert len(chosen) == 2
    
    # Linker should successfully merge both
    ctx = Linker.link(ExecutionContext(model_id=MODEL), chosen, gov)
    assert np.linalg.norm(ctx.M) <= gov.norm_budget
    assert "medical.Diagnosis" in set().union(*[c.capabilities for c in chosen])
    assert "radiology.Read" in set().union(*[c.capabilities for c in chosen])

def test_scheduler_cost_hook():
    d, MODEL = 64, "grafomem-v0"
    reg = Registry()
    
    # Both provide A, but c1 is high-cost (alpha=10) and c2 is low-cost (alpha=1)
    c1 = _cso(d, ["medical.Diagnosis"], MODEL, 1)
    c1.alpha = 10.0
    reg.register(c1)
    
    c2 = _cso(d, ["medical.Diagnosis"], MODEL, 2)
    c2.alpha = 1.0
    reg.register(c2)
    
    gov = Governance(MODEL, norm_budget=2.0)
    sel = Selector(reg)
    cands = sel.by_capability(["medical.Diagnosis"], model_id=MODEL)
    
    # Scheduler with cost=alpha should pick c2
    sched = Scheduler(gov, cost_fn=lambda c: c.alpha)
    chosen = sched.schedule(cands, ["medical.Diagnosis"])
    assert len(chosen) == 1
    assert chosen[0].alpha == 1.0
    
    # Scheduler with cost=-alpha should pick c1
    sched_inv = Scheduler(gov, cost_fn=lambda c: -c.alpha)
    chosen_inv = sched_inv.schedule(cands, ["medical.Diagnosis"])
    assert len(chosen_inv) == 1
    assert chosen_inv[0].alpha == 10.0

def test_greedy_known_gap():
    # known greedy limitation, not a bug — pins behavior; revisit if an exact solver is added.
    d, MODEL = 64, "grafomem-v0"
    reg = Registry()
    
    c_bulky = _cso(d, ["A", "B", "C"], MODEL, 1)
    # Give bulky a high utility but make it eat almost all the budget
    c_bulky.M = c_bulky.M / np.linalg.norm(c_bulky.M) * 1.95
    reg.register(c_bulky)
    
    # These two jointly cover everything perfectly under budget
    c_small1 = _cso(d, ["A", "B"], MODEL, 2)
    c_small1.M = c_small1.M / np.linalg.norm(c_small1.M) * 0.5
    reg.register(c_small1)
    
    c_small2 = _cso(d, ["C", "D"], MODEL, 3)
    c_small2.M = c_small2.M / np.linalg.norm(c_small2.M) * 0.5
    reg.register(c_small2)
    
    gov = Governance(MODEL, norm_budget=2.0)
    sel = Selector(reg)
    required = ["A", "B", "C", "D"]
    cands = sel.by_capability(required, model_id=MODEL)
    
    sched = Scheduler(gov)
    
    try:
        sched.schedule(cands, required)
        assert False, "Greedy should have failed due to limitation"
    except InfeasibleSchedule as e:
        assert "greedy could not cover" in str(e)
        assert "{'D'}" in str(e)
        assert "within V=2.0" in str(e)

def test_mcp_roundtrip():
    c = _cso(64, ["medical.Diagnosis"], "grafomem-v0", 7)
    c2 = from_mcp_resource(to_mcp_resource(c, _priv), _trusted)
    assert np.allclose(c.M, c2.M) and c.capabilities == c2.capabilities

def test_m4_loader():
    d, MODEL = 64, "grafomem-v0"
    reg = Registry()
    c1 = _cso(d, ["medical.Diagnosis"], MODEL, 1)
    reg.register(c1)
    gov = Governance(MODEL, norm_budget=2.0)
    
    ctx = Linker.link(ExecutionContext(model_id=MODEL), [c1], gov)
    q = np.random.default_rng(0).standard_normal(d)
    y1 = ctx.run(q)
    
    # 1. Checkpoint -> .gfm (signed) and carries capabilities
    gfm_bytes = Loader.checkpoint(ctx, MODEL, _priv, key_id="key1")
    
    # 2. Hot-swap load -> verify .gfm signature + type/policy, then link
    ctx_new = ExecutionContext(model_id=MODEL)
    ctx_new = Loader.load(ctx_new, gfm_bytes, gov, _trusted)
    y2 = ctx_new.run(q)
    
    assert np.allclose(y1, y2)
    assert ctx_new.capabilities == {"medical.Diagnosis"}
    
    # 3. Erase -> signed receipt
    rcpt = Loader.erase(ctx, "all", private_key=_priv, key_id="key1")
    assert rcpt.verify(_pub), "Erase receipt signature must verify"
    assert np.allclose(ctx.M, np.zeros_like(ctx.M))

def test_blob_roundtrip_and_tamper():
    c = CSO(M=None, blob=b"my_blob_data", payload_type="blob", model_id="grafomem-v1", capabilities=frozenset(["namespace.cap1"]),
            consent={"subject_id": "u1", "policy": "tenant"}, key_id="key1")
    gfm = c.to_gfm(_priv)
    
    # 1. Roundtrip
    c2 = CSO.from_gfm(gfm, _trusted)
    assert c2.payload_type == "blob"
    assert c2.blob == b"my_blob_data"
    assert c2.M is None
    
    # 2. Tampered blob (signature mismatch)
    b_arr = bytearray(gfm)
    b_arr[-1] ^= 0xFF
    try:
        CSO.from_gfm(bytes(b_arr), _trusted)
        assert False, "Should reject tampered blob"
    except ValueError as e:
        assert "signature mismatch" in str(e)
        
    # 3. Discriminating tamper (edit header to expired consent)
    import json, struct
    b_arr = bytearray(gfm)
    hl = struct.unpack("<I", b_arr[4:8])[0]
    header_str = b_arr[8:8+hl].decode('utf-8')
    header_dict = json.loads(header_str)
    header_dict["consent"]["expires_at"] = "2000-01-01T00:00:00Z"
    new_header = json.dumps(header_dict, separators=(',', ':')).encode('utf-8')
    # Better yet, just flip a byte in the consent policy string
    b_arr = bytearray(gfm)
    # Find "tenant" and change to "renant"
    idx = b_arr.find(b'"tenant"')
    if idx != -1:
        b_arr[idx+1] = ord('r')
        
        try:
            CSO.from_gfm(bytes(b_arr), _trusted)
            assert False, "Should reject header tamper before checking policy"
        except ValueError as e:
            assert "signature mismatch" in str(e)

def test_blob_signer_independence():
    c = CSO(M=None, blob=b"my_blob_data", payload_type="blob", model_id="grafomem-v1", capabilities=frozenset(["namespace.cap1"]),
            consent={"subject_id": "u1", "policy": "tenant"}, key_id="key1")
    
    # Sign with priv1
    gfm1 = c.to_gfm(_priv)
    
    # Sign with priv2
    priv2 = ed25519.Ed25519PrivateKey.generate()
    c.key_id = "key2"
    gfm2 = c.to_gfm(priv2)
    
    trusted = {"key1": _pub, "key2": priv2.public_key()}
    
    c1 = CSO.from_gfm(gfm1, trusted)
    c2 = CSO.from_gfm(gfm2, trusted)
    
    assert c1.content_hash() == c2.content_hash()

def test_blob_read_raises():
    c = CSO(M=None, blob=b"my_blob_data", payload_type="blob", model_id="grafomem-v1", capabilities=frozenset(["namespace.cap1"]),
            consent={"subject_id": "u1", "policy": "tenant"}, key_id="key1")
    import pytest
    with pytest.raises(TypeError, match="Cannot read"):
        c.read(np.ones((8,)))

if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn): fn(); print(f"ok  {name}")
    print("all tests passed.")
