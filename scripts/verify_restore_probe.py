#!/usr/bin/env python3
"""
GRAFOMEM W6 Restore Probe (Erasure Certification Guard)

This probe is mandatory during the Backup / Disaster Recovery procedure.
When a database is restored from a snapshot, there is a severe risk of "resurrecting" 
data that was legally and cryptographically erased *after* the snapshot was taken.

This script leverages the W6 deletion primitive logic to verify that:
  1. The erasure_certificates ledger is read.
  2. Every fact_ref that holds a certificate is completely absent from the 
     primary `memories` table AND the `memory_embeddings` table.

If any resurrected data is found, the script exits with a non-zero status code,
preventing the restored database from being added to the production routing pool.
"""

import argparse
import sys
import logging
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    print("Error: psycopg is required. Install with: pip install psycopg")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("W6-Restore-Probe")

def verify_restored_state(db_url: str) -> bool:
    """Verifies that no erased facts have been resurrected in the database."""
    logger.info("Connecting to recovery database...")
    try:
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            # 1. Fetch all certificates
            logger.info("Fetching erasure ledger...")
            certs = conn.execute(
                "SELECT certificate_id, tenant_id, fact_ref "
                "FROM erasure_certificates"
            ).fetchall()
            
            if not certs:
                logger.info("No erasure certificates found in this snapshot. PASS.")
                return True

            logger.info(f"Found {len(certs)} erasure certificates. Probing for resurrections...")
            
            resurrections = []
            
            # 2. Probe for resurrections in the primary and embedding tables
            for cert in certs:
                tenant_id = cert["tenant_id"]
                fact_ref = cert["fact_ref"]
                cert_id = cert["certificate_id"]
                
                # Check primary memories table
                primary_row = conn.execute(
                    "SELECT 1 FROM memories WHERE tenant_id = %s AND ref = %s",
                    (tenant_id, fact_ref)
                ).fetchone()
                
                # Check memory embeddings table
                embed_row = conn.execute(
                    "SELECT 1 FROM memory_embeddings WHERE tenant_id = %s AND ref = %s",
                    (tenant_id, fact_ref)
                ).fetchone()
                
                if primary_row:
                    resurrections.append({
                        "cert_id": cert_id,
                        "tenant": tenant_id,
                        "ref": fact_ref,
                        "store": "memories (primary)"
                    })
                
                if embed_row:
                    resurrections.append({
                        "cert_id": cert_id,
                        "tenant": tenant_id,
                        "ref": fact_ref,
                        "store": "memory_embeddings"
                    })

            # 3. Report findings
            if resurrections:
                logger.error(f"CRITICAL: {len(resurrections)} erased facts have been RESURRECTED by this snapshot!")
                for r in resurrections:
                    logger.error(f"  - Tenant: {r['tenant']} | Ref: {r['ref']} | Found in: {r['store']} | Cert: {r['cert_id']}")
                logger.error("Do NOT route traffic to this instance. You must run the ErasureSweeper to re-enforce the ledger.")
                return False
            else:
                logger.info("W6 Probe Complete: PASS (NO LEAKS). No resurrections detected.")
                return True

    except Exception as e:
        logger.error(f"Failed to execute probe: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="GRAFOMEM W6 Restore Probe")
    parser.add_argument("--db-url", required=True, help="PostgreSQL connection URI for the restored database")
    args = parser.parse_args()

    success = verify_restored_state(args.db_url)
    if not success:
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
