import json
import logging
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from aml.cloud.erasure_proof import ErasureProofService, verify_erasure_effect
from aml.cloud.erasure_sweeper import ErasureSweeper

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("demo_w9_erasure")

DB_URL = "postgresql://camiloayerbeposada@localhost:5432/postgres"

# Generate a signing key for the demo
seed = Ed25519PrivateKey.generate().private_bytes(
    Encoding.Raw, PrivateFormat.Raw, NoEncryption()
)

class DemoIdentity:
    def sign(self, message: bytes):
        priv = Ed25519PrivateKey.from_private_bytes(seed)
        from cryptography.hazmat.primitives.serialization import PublicFormat
        return priv.sign(message), priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        
    def public_key(self):
        priv = Ed25519PrivateKey.from_private_bytes(seed)
        from cryptography.hazmat.primitives.serialization import PublicFormat
        return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def setup_data():
    """Insert a memory and its embedding."""
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        # Cleanup
        conn.execute("TRUNCATE demo_erasure_certificates CASCADE")
        conn.execute("TRUNCATE demo_memory_embeddings CASCADE")
        conn.execute("TRUNCATE demo_memories CASCADE")

        res = conn.execute(
            "INSERT INTO demo_memories (content, tenant_id) VALUES (%s, %s) RETURNING ref",
            ("User H secret data", "tenant_demo")
        ).fetchone()
        ref = res[0]
        
        # Insert a dummy embedding
        dummy_vec = "[" + ",".join(["0.1"] * 1536) + "]"
        conn.execute(
            "INSERT INTO demo_memory_embeddings (ref, embedding, tenant_id) VALUES (%s, %s, %s)",
            (ref, dummy_vec, "tenant_demo")
        )
        return ref


def run_demo():
    print("\n--- W9 Erasure Architecture Demo ---\n")
    ref = setup_data()
    tenant_id = "tenant_demo"
    print(f"1. Setup: Created primary memory and embedding (ref={ref})")

    # Act 1: Delete primary, leave embedding, issue certificate
    print("\n--- Act 1: Primary Deletion (Orphaned Embedding) ---")
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM demo_memories WHERE ref = %s", (ref,))
        conn.execute("UPDATE demo_memory_embeddings SET erasure_pending = now() WHERE ref = %s", (ref,))
    print("2. Deleted primary memory. Marked embedding as erasure_pending.")

    # Check embedding is still resolvable
    with psycopg.connect(DB_URL) as conn:
        n = conn.execute("SELECT COUNT(*) FROM demo_memory_embeddings WHERE ref = %s", (ref,)).fetchone()[0]
        assert n == 1, "Embedding should still exist!"
    print("3. Verified embedding is still resolvable (orphaned-but-resolvable).")

    # Issue Certificate
    proof_svc = ErasureProofService(db_url=DB_URL, signing_identity=DemoIdentity(), table_prefix="demo_")
    proof_svc.ensure_schema()
    
    # The coverage tells the truth: embedding is still there
    coverage_act1 = {"primary": "absent", "embedding": "present", "cache": "unchecked"}
    cert_1 = proof_svc.issue_certificate(
        tenant_id=tenant_id,
        fact_ref=ref,
        fact_content="User H secret data",
        coverage=coverage_act1,
    )
    print(f"4. Issued Erasure Certificate {cert_1.certificate_id}")
    print(f"   Coverage explicitly recorded as: {coverage_act1}")

    # Recompute-from-bytes independent verification
    cert_dict_1 = ErasureProofService.cert_to_dict(cert_1)
    status_dict_1 = verify_erasure_effect(
        cert_bytes=json.dumps(cert_dict_1).encode('utf-8'),
        signature=cert_1.signature,
        public_key=cert_1.public_key,
        required_stores=["primary", "embedding", "cache"]
    )
    print(f"5. Independent Verifier (from bytes): {status_dict_1['result']} | gaps: {status_dict_1['coverage_gaps']}")
    assert status_dict_1['result'] == "failed", "Verifier MUST return 'failed' because embedding is present, dominating unchecked cache."
    assert "cache" in status_dict_1['coverage_gaps'], "cache must be in coverage_gaps"

    # Act 2: Async Sweeper runs
    print("\n--- Act 2: Async Sweeper (Right to Be Forgotten completed) ---")
    sweeper = ErasureSweeper(db_url=DB_URL, window_minutes=0, table_prefix="demo_") # 0 min for immediate sweep
    swept_count = sweeper.sweep()
    print(f"6. Sweeper ran and deleted {swept_count} orphaned embeddings.")

    # Issue updated Certificate
    coverage_act2 = {"primary": "absent", "embedding": "absent", "cache": "unchecked"}
    cert_2 = proof_svc.issue_certificate(
        tenant_id=tenant_id,
        fact_ref=ref,
        fact_content="User H secret data",
        coverage=coverage_act2,
    )
    print(f"7. Issued updated Erasure Certificate {cert_2.certificate_id}")
    print(f"   Coverage explicitly recorded as: {coverage_act2}")

    # Recompute-from-bytes independent verification
    cert_dict_2 = ErasureProofService.cert_to_dict(cert_2)
    status_dict_2 = verify_erasure_effect(
        cert_bytes=json.dumps(cert_dict_2).encode('utf-8'),
        signature=cert_2.signature,
        public_key=cert_2.public_key,
        required_stores=["primary", "embedding", "cache"]
    )
    print(f"8. Independent Verifier (from bytes): {status_dict_2['result']} | gaps: {status_dict_2['coverage_gaps']}")
    # cache is unchecked -> incomplete
    assert status_dict_2['result'] == "incomplete", "Verifier MUST return 'incomplete' because cache is unchecked."
    assert "cache" in status_dict_2['coverage_gaps'], "cache must be in coverage_gaps"

    # Act 3: Final check with cache checked
    print("\n--- Act 3: Full compliance (Cache checked) ---")
    coverage_act3 = {"primary": "absent", "embedding": "absent", "cache": "absent"}
    cert_3 = proof_svc.issue_certificate(
        tenant_id=tenant_id,
        fact_ref=ref,
        fact_content="User H secret data",
        coverage=coverage_act3,
    )
    cert_dict_3 = ErasureProofService.cert_to_dict(cert_3)
    status_dict_3 = verify_erasure_effect(
        cert_bytes=json.dumps(cert_dict_3).encode('utf-8'),
        signature=cert_3.signature,
        public_key=cert_3.public_key,
        required_stores=["primary", "embedding", "cache"]
    )
    print(f"9. Independent Verifier (cache now absent): {status_dict_3['result']} | gaps: {status_dict_3['coverage_gaps']}")
    assert status_dict_3['result'] == "enforced", "Verifier MUST return 'enforced' when all stores absent."
    assert status_dict_3['coverage_gaps'] == [], "coverage_gaps must be empty for enforced"

    print("\n--- Test signature tampering ---")
    # Tamper the coverage
    cert_dict_3_tampered = cert_dict_3.copy()
    cert_dict_3_tampered["coverage"] = {"primary": "absent", "embedding": "absent", "cache": "unchecked"}
    status_dict_tampered = verify_erasure_effect(
        cert_bytes=json.dumps(cert_dict_3_tampered).encode('utf-8'),
        signature=cert_3.signature,
        public_key=cert_3.public_key,
        required_stores=["primary", "embedding", "cache"]
    )
    print(f"10. Tampered independent Verifier: {status_dict_tampered['result']} | gaps: {status_dict_tampered['coverage_gaps']}")
    assert status_dict_tampered['result'] == "invalid", "Verifier MUST return 'invalid' on signature mismatch."
    assert status_dict_tampered['coverage_gaps'] == [], "coverage_gaps must be empty for invalid"
    
    print("\nDemonstrator successful.")


if __name__ == "__main__":
    run_demo()
