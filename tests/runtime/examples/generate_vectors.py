import os
import json
import numpy as np
from datetime import datetime, timedelta, timezone
from cryptography.hazmat.primitives.asymmetric import ed25519
from grafomem import CSO

def generate_vectors(out_dir: str):
    os.makedirs(out_dir, exist_ok=True)

    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key()
    
    # Write the public key out so TS can use it
    pub_bytes = pub.public_bytes(
        encoding=__import__('cryptography.hazmat.primitives.serialization').hazmat.primitives.serialization.Encoding.Raw,
        format=__import__('cryptography.hazmat.primitives.serialization').hazmat.primitives.serialization.PublicFormat.Raw
    )
    with open(os.path.join(out_dir, "trusted_key.bin"), "wb") as f:
        f.write(pub_bytes)
        
    M = np.ones((4, 4), dtype=np.float32) * 0.1

    # 1. valid.gfm
    cso_valid = CSO(M=M, model_id="grafomem-v1", capabilities=frozenset(["namespace.cap1"]), 
                    consent={"subject_id": "u1", "policy": "tenant"}, key_id="key1")
    with open(os.path.join(out_dir, "valid.gfm"), "wb") as f:
        f.write(cso_valid.to_gfm(priv))

    # 2. tampered.gfm
    gfm = bytearray(cso_valid.to_gfm(priv))
    gfm[-1] ^= 0xFF
    with open(os.path.join(out_dir, "tampered.gfm"), "wb") as f:
        f.write(gfm)

    # 3. expired-consent.gfm
    exp_time = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    cso_exp = CSO(M=M, model_id="grafomem-v1", capabilities=frozenset(["namespace.cap1"]), 
                  consent={"subject_id": "u1", "policy": "tenant", "expires_at": exp_time}, key_id="key1")
    with open(os.path.join(out_dir, "expired-consent.gfm"), "wb") as f:
        f.write(cso_exp.to_gfm(priv))

    # 4. unknown-key.gfm
    cso_unk = CSO(M=M, model_id="grafomem-v1", capabilities=frozenset(["namespace.cap1"]), 
                  consent={"subject_id": "u1", "policy": "tenant"}, key_id="unknown")
    with open(os.path.join(out_dir, "unknown-key.gfm"), "wb") as f:
        f.write(cso_unk.to_gfm(priv)) # signed with valid priv, but claims unknown key
        
    # 5. valid-blob.gfm
    cso_blob = CSO(M=None, blob=b"hello_world", payload_type="blob", model_id="grafomem-v1", 
                   capabilities=frozenset(["namespace.cap1"]), consent={"subject_id": "u1", "policy": "tenant"}, key_id="key1")
    with open(os.path.join(out_dir, "valid-blob.gfm"), "wb") as f:
        f.write(cso_blob.to_gfm(priv))

    # 6. tampered-blob.gfm
    blob_gfm = bytearray(cso_blob.to_gfm(priv))
    blob_gfm[-1] ^= 0xFF
    with open(os.path.join(out_dir, "tampered-blob.gfm"), "wb") as f:
        f.write(blob_gfm)

    print(f"Generated test vectors in {out_dir}")

if __name__ == "__main__":
    generate_vectors("tests/vectors")
