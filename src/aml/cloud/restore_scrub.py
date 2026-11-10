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
) -> dict[str, Any]:
    """
    Reapply the external Erasure Ledger to an active database.
    This ensures that any data subjects or tenant keys that were legally 
    erased prior to a backup restore remain erased and inert in the active DB.
    
    Returns a dict with statistics of actions taken.
    """
    logger.info("Starting restore-scrub runbook...")
    
    stats: dict[str, Any] = {
        "tenant_keys_destroyed": 0,
        "subject_memories_deleted": 0,
        "decision_records_scrubbed": 0,
        # 0019 F4: data-subject keys a restore brought back, re-destroyed from the ledger
        "data_subject_keys_redestroyed": 0,
        "data_subject_keys_already_gone": 0,
        # 0017: the chain is verified BEFORE replay and the verdict travels with the
        # stats. A broken chain is reported loudly but replay still proceeds — refusing
        # would leave revived data un-scrubbed, which is worse than replaying a ledger
        # known to be incomplete. Operators act on `ledger_chain_problems`.
        "ledger_chain_ok": None,
        "ledger_chain_problems": [],
    }

    try:
        ok, problems = erasure_ledger.verify()
    except Exception as e:  # a verifier failure must not block the scrub
        ok, problems = False, [f"verification failed to run: {e}"]
    stats["ledger_chain_ok"] = ok
    stats["ledger_chain_problems"] = problems
    if ok:
        logger.info("Erasure ledger chain verified OK")
    else:
        logger.error("Erasure ledger chain FAILED verification (%d problem(s)); replaying what "
                     "is present anyway: %s", len(problems), problems)

    erasures = erasure_ledger.get_all_erasures()  # excludes the chain_genesis row
    
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
                
        elif entry_type == "data_subject_key_destroyed":
            # 0019: re-assert the destruction of a data-subject key by its fingerprint. The
            # certificate payload carries fingerprints only; the key store is looked up by
            # the KEY fingerprint, so a restored row (wrapped_dek back, destroyed_at NULL)
            # is found and tombstoned again. Facts of that subject are re-deleted by their
            # own subject_erasure entries (one per fact), as before.
            payload = entry.get("certificate") or {}
            if isinstance(payload, str):
                import json as _json
                payload = _json.loads(payload)
            fp = payload.get("key_fingerprint")
            if fp and payload.get("key_destroyed"):
                try:
                    if tenant_key_manager.redestroy_data_subject_key_by_fingerprint(tenant_id, fp):
                        logger.warning("Re-destroyed data-subject key %s for tenant=%s (a restore had revived it)", fp, tenant_id)
                        stats["data_subject_keys_redestroyed"] += 1
                    else:
                        stats["data_subject_keys_already_gone"] += 1
                except Exception as e:
                    logger.warning("Failed to re-destroy data-subject key %s: %s", fp, e)

        elif entry_type == "chain_genesis":
            # 0017 sealing row — chain metadata, nothing to replay.
            continue
        else:
            logger.warning("Unknown erasure ledger entry_type=%s, ignoring", entry_type)

    logger.info("Completed restore-scrub runbook. Stats: %s", stats)
    return stats
