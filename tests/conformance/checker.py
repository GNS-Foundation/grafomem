import os
import sys
import json
import base64
import struct
import tempfile
import asyncio
import numpy as np
import traceback
from datetime import datetime, timezone, timedelta
from cryptography.hazmat.primitives.asymmetric import ed25519

from grafomem.cso import CSO, GFM_MAGIC
from grafomem.runtime import Registry, Governance, Selector, Scheduler, Linker, Loader, ExecutionContext
from grafomem.errors import SignatureMismatch, UnknownKey, PolicyViolation
from grafomem.mcp import from_mcp_resource, MCP_CONTENT_TYPE
from grafomem.server import create_server

class ConformanceSuite:
    def __init__(self):
        self.priv = ed25519.Ed25519PrivateKey.generate()
        self.pub = self.priv.public_key()
        self.trusted = {"test-key": self.pub}
        self.temp_dir = tempfile.mkdtemp()
        self.registry = Registry(directory=self.temp_dir)
        self.failures = []

    def report(self, clause_id, desc, success, error=""):
        status = "PASS" if success else "FAIL"
        msg = f"[{status}] Clause {clause_id}: {desc}"
        if not success:
            msg += f" -> {error}"
            self.failures.append(clause_id)
        print(msg)

    def _cso(self, capabilities, seed=1, M_val=None, policy="tenant"):
        d = 64
        if M_val is None:
            # small norm to pass V=1.0
            M = np.zeros((d, d), dtype=np.float32)
        else:
            M = M_val
        consent = {"subject_id": "did:user", "policy": policy}
        return CSO(M=M, model_id="grafomem-v0", capabilities=frozenset(capabilities), consent=consent, key_id="test-key")

    async def run_all(self):
        print("=== Grafomem SPEC §7 Conformance Suite ===\n")
        
        try:
            self.check_a_signature_integrity()
        except Exception as e:
            self.report("a", "Signature integrity", False, traceback.format_exc())

        try:
            self.check_b_feasibility_policy()
        except Exception as e:
            self.report("b", "Feasibility + policy", False, traceback.format_exc())
            
        try:
            self.check_c_no_introspection()
        except Exception as e:
            self.report("c", "No-introspection routing", False, traceback.format_exc())

        try:
            await self.check_d_mcp_contract()
        except Exception as e:
            self.report("d", "MCP contract carriage", False, traceback.format_exc())

        try:
            self.check_e_erasure_semantics()
        except Exception as e:
            self.report("e", "Erasure semantics", False, traceback.format_exc())

        try:
            self.check_f_research_stubs()
        except Exception as e:
            self.report("f", "Research stubs", False, traceback.format_exc())
            
        print("\n=== Report Summary ===")
        if len(self.failures) == 0:
            print("ALL PASSED: Conformance verified.")
            return 0
        else:
            print(f"FAILED CLAUSES: {', '.join(self.failures)}")
            return 1

    def check_a_signature_integrity(self):
        # positive: valid loads
        c1 = self._cso(["medical.Diagnosis"])
        valid_bytes = c1.to_gfm(self.priv)
        
        try:
            loaded = CSO.from_gfm(valid_bytes, self.trusted)
        except Exception as e:
            self.report("a", "Valid load failed", False, str(e))
            return
            
        # negative: tampered -> signature mismatch
        tampered_bytes = bytearray(valid_bytes)
        tampered_bytes[-1] ^= 0xFF # Flip last byte of signature
        try:
            CSO.from_gfm(bytes(tampered_bytes), self.trusted)
            self.report("a", "Tampered tensor loaded silently", False)
            return
        except SignatureMismatch as e:
            if "signature mismatch" not in str(e).lower():
                self.report("a", "Wrong error on tampered signature", False, str(e))
                return
                
        # negative: unknown key -> unknown key_id
        c2 = CSO(M=c1.M, model_id=c1.model_id, capabilities=c1.capabilities, consent=c1.consent, key_id="unknown-key")
        unknown_bytes = c2.to_gfm(self.priv) # signed with private, but claims unknown-key
        try:
            CSO.from_gfm(unknown_bytes, self.trusted)
            self.report("a", "Unknown key loaded silently", False)
            return
        except UnknownKey as e:
            if "unknown key" not in str(e).lower():
                self.report("a", "Wrong error on unknown key", False, str(e))
                return
                
        self.report("a", "Signature integrity two-sided check passed", True)

    def check_b_feasibility_policy(self):
        c1 = self._cso(["a"])
        c2 = self._cso(["b"])
        # We need an over-budget set. V = 1.0 norm budget.
        M_huge = np.ones((64, 64), dtype=np.float32) * 10.0 # norm = sqrt(64*64*100) = 640 > 1.0
        c_huge = CSO(M=M_huge, model_id="grafomem-v0", capabilities=frozenset(["huge"]), consent={"subject_id": "did", "policy": "tenant"}, key_id="test-key")
        
        gov = Governance("grafomem-v0", 1.0)
        ctx = ExecutionContext(model_id="grafomem-v0")
        
        try:
            Linker.link(ctx, [c_huge], gov)
            self.report("b", "Over-budget set loaded silently", False)
            return
        except PolicyViolation as e:
            if "inadmissible link" not in str(e):
                self.report("b", "Wrong error on over-budget", False, str(e))
                return

        c_policy_fail = self._cso(["private_cap"], policy="private")
        try:
            Linker.link(ctx, [c_policy_fail], gov)
            self.report("b", "Policy violation loaded silently", False)
            return
        except PolicyViolation as e:
            if "inadmissible link" not in str(e):
                self.report("b", "Wrong error on policy violation", False, str(e))
                return
                
        self.report("b", "Feasibility + policy refusal asserted exactly", True)

    def check_c_no_introspection(self):
        # Two CSOs, identical headers, different M
        c1 = self._cso(["cap1"], M_val=np.ones((64, 64), dtype=np.float32))
        c2 = self._cso(["cap1"], M_val=np.zeros((64, 64), dtype=np.float32))
        
        reg = Registry(directory=None)
        reg.register(c1)
        reg.register(c2)
        
        sel = Selector(reg)
        candidates = sel.by_capability({"cap1"}, model_id="grafomem-v0")
        
        if len(candidates) != 2:
            self.report("c", f"Routing failed to find both candidates (found {len(candidates)})", False)
            return
            
        # Assert they are identically routed (both included as valid candidates)
        self.report("c", "No-introspection routing identical selection passed", True)

    async def check_d_mcp_contract(self):
        c1 = self._cso(["real.capability"])
        valid_bytes = c1.to_gfm(self.priv)
        
        # 1. Tamper the header bytes directly without fixing the signature
        o = 4
        hl = struct.unpack("<I", valid_bytes[o:o+4])[0]
        o += 4
        header_json = valid_bytes[o:o+hl].decode("utf-8")
        
        h_dict = json.loads(header_json)
        h_dict["capabilities"].append("fake.capability")
        new_header_bytes = json.dumps(h_dict).encode("utf-8")
        new_hl = len(new_header_bytes)
        
        tampered_bytes = bytearray(GFM_MAGIC)
        tampered_bytes.extend(struct.pack("<I", new_hl))
        tampered_bytes.extend(new_header_bytes)
        tampered_bytes.extend(valid_bytes[o+hl:])
        
        sid_tampered = "d_tampered"
        with open(os.path.join(self.temp_dir, f"{sid_tampered}.gfm"), "wb") as f:
            f.write(tampered_bytes)
            
        app = create_server(self.temp_dir)
        resources = await app.list_resources()
        
        target_res = next((r for r in resources if str(r.uri) == f"grafomem://cso/{sid_tampered}"), None)
        if target_res is None:
            self.report("d", "Server failed to list the tampered resource", False)
            return
            
        desc = json.loads(target_res.description)
        if "fake.capability" not in desc["capabilities"]:
            self.report("d", "Server did not advertise the inflated advisory capability", False)
            return
            
        # 2. Client side boundary test
        contents = await app.read_resource(str(target_res.uri))
        mcp_payload = json.loads(contents[0].content)
        mcp_payload["bytes"] = base64.b64decode(mcp_payload["bytes"])
        
        try:
            loaded_cso = from_mcp_resource(mcp_payload, self.trusted)
            self.report("d", "Lie survived the trust boundary! (loaded fake capability)", False)
            return
        except SignatureMismatch as e:
            if "signature mismatch" not in str(e).lower():
                self.report("d", f"Rejected for wrong reason: {str(e)}", False)
                return
                
        self.report("d", "MCP Contract: Advisory lie dies at the trust boundary", True)

    def check_e_erasure_semantics(self):
        c1 = self._cso(["a.cap"])
        gov = Governance("grafomem-v0", 1.0)
        ctx = ExecutionContext(model_id="grafomem-v0")
        ctx = Linker.link(ctx, [c1], gov)
        
        # Valid erase
        receipt = Loader.erase(ctx, "test-scope", self.priv, "server-key")
        
        if not receipt.verify(self.pub):
            self.report("e", "Valid receipt failed verification", False)
            return
            
        if np.any(ctx.M != 0):
            self.report("e", "State matrix M was not effectively zeroed", False)
            return
            
        # Negative test (mutate receipt)
        receipt.scope = "mutated-scope"
        if receipt.verify(self.pub):
            self.report("e", "Mutated receipt falsely passed verification", False)
            return
            
        self.report("e", "Erasure receipt verification two-sided test passed", True)

    def check_f_research_stubs(self):
        sel = Selector(Registry(directory=None))
        try:
            sel.behavioral(None)
            self.report("f", "Selector.behavioral did not raise NotImplementedError", False)
        except NotImplementedError as e:
            if "research" not in str(e).lower():
                self.report("f", "Stub error does not mention 'research'", False)
            else:
                self.report("f", "Research stubs remain honest and un-faked", True)
        except Exception as e:
            self.report("f", f"Wrong exception raised: {e.__class__.__name__}", False)

if __name__ == "__main__":
    suite = ConformanceSuite()
    sys.exit(asyncio.run(suite.run_all()))
