import json
from aml.cloud.erasure_proof import verify_erasure_effect, compute_certificate_digest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, PrivateFormat, NoEncryption

def run_tests():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    pub_bytes = pub.public_bytes(Encoding.Raw, PublicFormat.Raw)

    base_cert = {
        "certificate_id": "test",
        "tenant_id": "t",
        "fact_ref": 1,
        "fact_content_hash": "hash",
        "erasure_requested_at": "2023-01-01T00:00:00Z",
        "erasure_completed_at": "2023-01-01T00:00:00Z",
        "legal_basis": "GDPR",
    }
    
    required_stores = ['primary', 'embedding', 'cache', 'kv']
    
    def test_case(name, coverage, tamper=False):
        cert_data = base_cert.copy()
        cert_data["coverage"] = coverage
        digest = compute_certificate_digest(cert_data)
        sig = priv.sign(digest)
        
        # Pass bytes to verify_erasure_effect
        cert_bytes = json.dumps(cert_data).encode("utf-8")
        
        if tamper:
            # Change a bit in the signature
            sig = bytearray(sig)
            sig[0] ^= 1
            sig = bytes(sig)
            
        status_dict = verify_erasure_effect(
            cert_bytes=cert_bytes,
            signature=sig,
            public_key=pub_bytes,
            required_stores=required_stores
        )
        print(f"[{name}] -> {status_dict['result']} | gaps: {status_dict['coverage_gaps']}")

    test_case("failed (present in required store)", {"primary": "absent", "embedding": "present", "cache": "unchecked", "kv": "absent"})
    test_case("incomplete (unchecked in required store)", {"primary": "absent", "embedding": "absent", "cache": "unchecked", "kv": "absent"})
    test_case("incomplete (missing required store)", {"primary": "absent", "embedding": "absent", "cache": "absent"})
    test_case("enforced (all required stores absent)", {"primary": "absent", "embedding": "absent", "cache": "absent", "kv": "absent"})
    test_case("invalid (tampered signature)", {"primary": "absent", "embedding": "absent", "cache": "absent", "kv": "absent"}, tamper=True)
    test_case("incomplete (erasure_pending status falls through)", {"primary": "absent", "embedding": "erasure_pending", "cache": "absent", "kv": "absent"})
    test_case("enforced (present status on NON-required store does not fail)", {"primary": "absent", "embedding": "absent", "cache": "absent", "kv": "absent", "other_store": "present"})

if __name__ == "__main__":
    run_tests()
