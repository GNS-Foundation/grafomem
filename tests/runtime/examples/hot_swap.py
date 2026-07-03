import os
import numpy as np
from cryptography.hazmat.primitives.asymmetric import ed25519
from grafomem import CSO, Governance, ExecutionContext
from grafomem.sdk import load, migrate

def main():
    # 1. Setup Trust
    priv1 = ed25519.Ed25519PrivateKey.generate()
    pub1 = priv1.public_key()
    trusted_keys1 = {"device1-key": pub1}
    
    priv2 = ed25519.Ed25519PrivateKey.generate()
    pub2 = priv2.public_key()
    trusted_keys2 = {"device2-key": pub2}
    
    gov = Governance(model_id="grafomem-v1", norm_budget=10.0, allowed_consent=["tenant"])
    
    # 2. Load context on Device 1
    cso_data = CSO(M=np.ones((64, 64), dtype=np.float32) * 0.01, 
                   model_id="grafomem-v1", 
                   capabilities=frozenset(["user.personalization"]),
                   consent={"subject_id": "user_123", "policy": "tenant"},
                   key_id="device1-key")
    
    gfm_bytes1 = cso_data.to_gfm(priv1)
    ctx1 = load(gfm_bytes1, trusted_keys1, gov)
    print("Device 1 State initialized.")
    
    # Simulate usage
    ctx1.M *= 0.5
    
    # 3. Migrate State to Device 2 (Hot-Swap)
    print("Migrating state from Device 1 to Device 2...")
    # The migration takes the state from ctx1, signs it with Device 1's key,
    # and loads it securely into a new context using Device 2's trust store.
    # Note: the payload is signed by device1-key, so Device 2 must trust device1-key!
    
    # Device 2 trusts Device 1
    trusted_keys2["device1-key"] = pub1
    
    ctx2 = migrate(ctx1, private_key=priv1, key_id="device1-key", new_trusted_keys=trusted_keys2, new_gov=gov)
    
    print("State successfully hot-swapped to Device 2!")
    print(f"Device 2 Matrix matches Device 1: {np.allclose(ctx1.M, ctx2.M)}")

if __name__ == "__main__":
    main()
