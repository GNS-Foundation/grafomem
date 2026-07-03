import os
import numpy as np
from cryptography.hazmat.primitives.asymmetric import ed25519
from grafomem import CSO, Registry, Governance, ExecutionContext
from grafomem.sdk import compose, execute

def main():
    # 1. Setup Trust & Governance
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key()
    trusted_keys = {"admin": pub}
    
    gov = Governance(model_id="grafomem-v1", norm_budget=10.0, allowed_consent=["tenant"])
    
    # 2. Setup Registry & populate with capabilities
    registry = Registry(trusted_keys=trusted_keys)
    
    # Create and register a capability: math.Add
    cso_add = CSO(M=np.ones((64, 64), dtype=np.float32) * 0.01, 
                  model_id="grafomem-v1", 
                  capabilities=frozenset(["math.Add"]),
                  consent={"subject_id": "sys", "policy": "tenant"},
                  key_id="admin")
    registry.register(cso_add)
    
    # Create and register a capability: math.Multiply
    cso_mult = CSO(M=np.ones((64, 64), dtype=np.float32) * 0.02, 
                   model_id="grafomem-v1", 
                   capabilities=frozenset(["math.Multiply"]),
                   consent={"subject_id": "sys", "policy": "tenant"},
                   key_id="admin")
    registry.register(cso_mult)

    # 3. Dynamic Composition
    print("Dynamically composing ['math.Add', 'math.Multiply']...")
    ctx = ExecutionContext(model_id="grafomem-v1")
    ctx = compose(ctx, ["math.Add", "math.Multiply"], registry, gov)
    
    print(f"Successfully loaded {len(ctx.loaded)} CSOs.")
    print(f"Context now holds capabilities: {ctx.capabilities}")
    
    # 4. Execution
    q = np.random.randn(64).astype(np.float32)
    result = execute(ctx, q)
    print(f"Execution complete. Output shape: {result.shape}")

if __name__ == "__main__":
    main()
