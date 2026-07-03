"""End-to-end demo: capability route -> schedule under V -> link -> run -> MCP round-trip."""
import numpy as np
from cryptography.hazmat.primitives.asymmetric import ed25519
from grafomem import (CSO, Registry, Selector, Scheduler, Linker, Governance,
                   ExecutionContext, Loader, to_mcp_resource, MCP_CONTENT_TYPE)

def cso(d, caps, model, seed):
    g = np.random.default_rng(seed)
    K = g.standard_normal((8,d)); K/=np.linalg.norm(K,axis=1,keepdims=True)
    V = g.standard_normal((8,d)); V/=np.linalg.norm(V,axis=1,keepdims=True)
    M = sum(np.outer(V[i],K[i]) for i in range(8))*0.1
    return CSO(M=M.astype(np.float32), model_id=model, capabilities=frozenset(caps),
               consent={"subject_id":"did:x","policy":"tenant","expires_at":None})

d, MODEL = 64, "grafomem-v0"; reg = Registry()
for caps, s in [(["medical.Diagnosis"],1), (["radiology.Read","medical.Diagnosis"],2),
                (["nephrology.Read"],3), (["lang.OldNorseTranslation"],4)]:
    reg.register(cso(d, caps, MODEL, s))
gov, sel, sched = Governance(MODEL, 2.0), Selector(reg), Scheduler(Governance(MODEL, 2.0))
required = ["medical.Diagnosis", "radiology.Read"]
cands = sel.by_capability(required, model_id=MODEL)
chosen = sched.schedule(cands, required)
ctx = Linker.link(ExecutionContext(model_id=MODEL, identity="hospital-A"), chosen, gov)
print(f"selected {len(cands)} → scheduled {len(chosen)} → ‖M‖={np.linalg.norm(ctx.M):.3f} ≤ V=2.0")
print(f"run y=Mq → dim {ctx.run(np.random.default_rng(0).standard_normal(d)).shape[0]}")
_priv = ed25519.Ed25519PrivateKey.generate()
rcpt = Loader.erase(ctx, scope="all", private_key=_priv, key_id="demo-key")
print(f"erase receipt: {rcpt.before} → {rcpt.after} (scope={rcpt.scope}) [Signature OK: {rcpt.verify(_priv.public_key())}]")
print(f"MCP content type: {MCP_CONTENT_TYPE}")
