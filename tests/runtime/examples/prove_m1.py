import json
import struct
import numpy as np
from cryptography.hazmat.primitives.asymmetric import ed25519
from grafomem.cso import CSO, GFM_VERSION, GFM_MAGIC
from datetime import datetime, timezone, timedelta

def prove():
    print(f"GFM_VERSION = {GFM_VERSION}")
    
    # 2) same state under two key_ids -> equal content_hash
    d = 2
    M = np.eye(d)
    c1 = CSO(M=M, model_id="test", capabilities=frozenset(["a.B"]), key_id="key-A")
    c2 = CSO(M=M, model_id="test", capabilities=frozenset(["a.B"]), key_id="key-B")
    h1 = c1.content_hash()
    h2 = c2.content_hash()
    print(f"c1 content_hash: {h1}")
    print(f"c2 content_hash: {h2}")
    print(f"Hashes equal? {h1 == h2}")
    
    # 3) The discriminating tamper: edit header to expired consent.expires_at
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key()
    trusted = {"test-key": pub}
    
    # Sign a valid .gfm
    future_date = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    c_valid = CSO(M=M, model_id="test", consent={"policy": "tenant", "expires_at": future_date}, key_id="test-key")
    
    valid_bytes = c_valid.to_gfm(priv)
    
    # Parse to find header length
    o = 4
    hl = struct.unpack("<I", valid_bytes[o:o+4])[0]
    o += 4
    header_json = valid_bytes[o:o+hl].decode("utf-8")
    
    # Modify header JSON
    h_dict = json.loads(header_json)
    expired_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    h_dict["consent"]["expires_at"] = expired_date
    
    new_header_bytes = json.dumps(h_dict).encode("utf-8")
    new_hl = len(new_header_bytes)
    
    # Reconstruct the gfm bytearray with tampered header
    tampered_bytes = bytearray(GFM_MAGIC)
    tampered_bytes.extend(struct.pack("<I", new_hl))
    tampered_bytes.extend(new_header_bytes)
    
    # Append the rest of the original bytes (tensor length + tensor + sig)
    # The original rest starts at o + hl
    rest_of_bytes = valid_bytes[o+hl:]
    tampered_bytes.extend(rest_of_bytes)
    
    print("\n--- Tamper Test ---")
    try:
        CSO.from_gfm(bytes(tampered_bytes), trusted)
        print("FAILED: Loaded successfully")
    except ValueError as e:
        print(f"Exception raised: {str(e)}")
        if "signature mismatch" in str(e):
            print("SUCCESS: Threw 'signature mismatch'")
        elif "Consent expired" in str(e):
            print("FAILED: Threw 'Consent expired' (validate-before-verify bug is present)")
        else:
            print(f"FAILED: Threw unexpected error: {str(e)}")

if __name__ == "__main__":
    prove()
