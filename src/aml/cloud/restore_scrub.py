import logging
from typing import Any

from aml.cloud.erasure_ledger import ErasureLedger
from aml.cloud.tenant_key_manager import TenantKeyManager
from aml.cloud.decision_trail import DecisionTrailService

logger = logging.getLogger("grafomem.cloud.restore_scrub")

def reapply_ledger(
    erasure_ledger: ErasureLedger,
    store_manager: Any,
    tenant_key_manager: TenantKeyManager,
    decision_trail: DecisionTrailService,
) -> dict[str, int]:
    """
    Reapply the external Erasure Ledger to an active database.
    This ensures that any data subjects or tenant keys that were legally 
    erased prior to a backup restore remain erased and inert in the active DB.
    
    Returns a dict with statistics of actions taken.
    """
    logger.info("Starting restore-scrub runbook...")
    
    stats = {
        "tenant_keys_destroyed": 0,
        "subject_memories_deleted": 0,
        "decision_records_scrubbed": 0,
    }
    
    erasures = erasure_ledger.get_all_erasures()
    
    for entry in erasures:
        tenant_id = entry["tenant_id"]
        entry_type = entry["entry_type"]
        
        if entry_type == "tenant_destruction":
            # Re-assert that this tenant's key is destroyed
            logger.info("Re-applying tenant_destruction for tenant=%s", tenant_id)
            tenant_key_manager.destroy_tenant_key(tenant_id)
            stats["tenant_keys_destroyed"] += 1
            
        elif entry_type == "subject_erasure":
            # Re-assert that this subject is scrubbed
            fact_ref = entry["fact_ref"]
            content_hash = entry["content_hash"]
            
            logger.info("Re-applying subject_erasure for tenant=%s, fact_ref=%s", tenant_id, fact_ref)
            
            # 1. Re-delete the fact from the store
            # StoreManager requires the store backend, so we need to get the backend
            try:
                store = store_manager._factory()
                if store.delete(fact_ref):
                    stats["subject_memories_deleted"] += 1
            except Exception as e:
                logger.warning("Failed to re-delete fact_ref=%s from store: %s", fact_ref, e)
                
            # 2. Re-scrub decision trail records
            try:
                # Get tenant encryption context to re-scrub encrypted trails
                encryption = tenant_key_manager.get_encryptor(tenant_id)
                scrubbed_count = decision_trail.scrub_fact(fact_ref, tenant_id, encryption=encryption)
                stats["decision_records_scrubbed"] += scrubbed_count
            except Exception as e:
                logger.warning("Failed to re-scrub decision records for fact_ref=%s: %s", fact_ref, e)
                
        else:
            logger.warning("Unknown erasure ledger entry_type=%s, ignoring", entry_type)

    logger.info("Completed restore-scrub runbook. Stats: %s", stats)
    return stats
