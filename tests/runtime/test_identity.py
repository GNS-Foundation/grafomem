import numpy as np
from cryptography.hazmat.primitives.asymmetric import ed25519
from grafomem.cso import CSO

priv1 = ed25519.Ed25519PrivateKey.generate()
priv2 = ed25519.Ed25519PrivateKey.generate()
pub1 = priv1.public_key()
pub2 = priv2.public_key()

c = CSO(M=None, blob=b"my_blob_data", payload_type="blob", model_id="grafomem-v1", capabilities=frozenset(["namespace.cap1"]), consent={"subject_id": "u1", "policy": "tenant"}, key_id="key1")

gfm1 = c.to_gfm(priv1)
c.key_id = "key2"
gfm2 = c.to_gfm(priv2)

trusted = {"key1": pub1, "key2": pub2}

c1 = CSO.from_gfm(gfm1, trusted)
c2 = CSO.from_gfm(gfm2, trusted)

import json, hashlib
payload1 = c1.M.astype("<f4").tobytes() if c1.payload_type == "tensor" and c1.M is not None else (c1.blob or b"")
payload2 = c2.M.astype("<f4").tobytes() if c2.payload_type == "tensor" and c2.M is not None else (c2.blob or b"")
print(payload1 == payload2)

h1 = hashlib.sha256(json.dumps(c1._identity(), sort_keys=True).encode() + payload1).hexdigest()
h2 = hashlib.sha256(json.dumps(c2._identity(), sort_keys=True).encode() + payload2).hexdigest()
print(h1, h2)
