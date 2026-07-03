import os
import numpy as np
from cryptography.hazmat.primitives.asymmetric import ed25519
from grafomem import CSO, Governance, ExecutionContext
from grafomem.sdk import load, erase

def main():
    # 1. Setup Trust
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key()
    trusted_keys = {"server-key": pub}
    
    gov = Governance(model_id="grafomem-v1", norm_budget=10.0, allowed_consent=["tenant"])
    
    # Create a signed payload
    cso_data = CSO(M=np.ones((64, 64), dtype=np.float32) * 0.01, 
                   model_id="grafomem-v1", 
                   capabilities=frozenset(["user.data"]),
                   consent={"subject_id": "user_123", "policy": "tenant"},
                   key_id="server-key")
    
    gfm_bytes = cso_data.to_gfm(priv)
    
    # 2. Load the payload into Execution Context
    ctx = load(gfm_bytes, trusted_keys, gov)
    print("User state loaded into memory.")
    print("Memory norm:", np.linalg.norm(ctx.M))
    
    # 3. User requests GDPR Erasure
    print("\nExecuting cryptographic erasure...")
    receipt = erase(ctx, scope="gdpr_request_42", private_key=priv, key_id="server-key")
    
    print("Memory norm after erasure:", np.linalg.norm(ctx.M))
    
    # 4. Verify the Receipt
    print("\nState-Transition Receipt Generated:")
    print(f"  Before Hash: {receipt.before}")
    print(f"  After Hash:  {receipt.after}")
    print(f"  Timestamp:   {receipt.timestamp}")
    
    is_valid = receipt.verify(pub)
    print(f"\nReceipt Cryptographically Valid? {is_valid}")

if __name__ == "__main__":
    main()
