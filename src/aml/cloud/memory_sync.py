"""
GRAFOMEM Memory Sync & Export Service — Sprint 29.

Provides the ability to export the entire semantic memory space (WorldModel 
types, actions, and raw Memory facts) into a cryptographically signed, portable JSON package.
Also provides additive sync (upsert) to ingest a memory package into a target environment.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from aml.cloud.world_model import WorldModelService
from aml.server.stores import StoreManager
from aml.cloud.audit_export import AuditExportService

logger = logging.getLogger("grafomem.cloud.memory_sync")


class MemorySyncService:
    def __init__(
        self,
        world_model: WorldModelService,
        store_manager: StoreManager,
        audit_export: AuditExportService,
    ) -> None:
        self._wm = world_model
        self._stores = store_manager
        self._audit = audit_export

    def export_memory(self, tenant_id: str, store_id: str) -> dict:
        """Export the complete knowledge graph for a tenant/store."""
        # 1. Fetch Ontological Types
        types = self._wm.list_types(tenant_id)
        
        # 2. Fetch memory facts
        store_entry = self._stores.get_or_404(store_id)
        memories = list(store_entry.backend.audit())
        
        # Serialize memories
        memory_list = []
        for m in memories:
            memory_list.append({
                "ref": str(m.ref),
                "content": m.content,
                "written_at": m.written_at.isoformat() if m.written_at else None,
                "metadata": m.metadata,
                "valid_from": m.valid_from.isoformat() if m.valid_from else None,
                "valid_until": m.valid_until.isoformat() if m.valid_until else None,
                "tenant_id": m.tenant_id,
                "superseded_by": str(m.superseded_by) if m.superseded_by else None,
            })

        payload = {
            "version": "grafomem_memory_v1",
            "tenant_id": tenant_id,
            "store_id": store_id,
            "exported_at": time.time(),
            "world_model": {
                "types": types,
            },
            "memories": memory_list,
        }

        # Wrap in cryptographic signatures using the AuditExport logic
        data_bytes = json.dumps(payload, default=str).encode("utf-8")
        result = self._audit._wrap(
            data_bytes, tenant_id, "memory_sync", "json",
            record_count=len(types) + len(memories),
            date_from=None, date_to=None,
            content_type="application/json"
        )
        
        return {
            "payload": payload,
            "metadata": {
                "content_hash": result.metadata.content_hash,
                "signature": result.metadata.signature,
                "exported_at": result.metadata.exported_at,
            }
        }

    def sync_memory(self, tenant_id: str, store_id: str, package: dict) -> dict:
        """Additive sync of an imported memory package into the current environment."""
        payload = package.get("payload", {})
        if not payload:
            raise ValueError("Invalid memory package: missing payload.")
        
        # 1. Additive Sync for WorldModel Types
        wm_data = payload.get("world_model", {})
        types = wm_data.get("types", [])
        synced_types = 0
        for t in types:
            try:
                # Upsert type
                self._wm.register_type(
                    tenant_id=tenant_id,
                    kind=t["kind"],
                    name=t["name"],
                    spec=t["spec"]
                )
                synced_types += 1
            except Exception as e:
                logger.warning(f"Failed to sync type {t.get('name')}: {e}")

        # 2. Additive Sync for Memories
        memories = payload.get("memories", [])
        store_entry = self._stores.get_or_404(store_id)
        synced_memories = 0
        
        # To avoid duplicates, we could either check if content exists, or just write.
        # Since memories are immutable facts, additive sync means we just write them.
        for m in memories:
            from aml.backends.interface import WriteOptions
            from dateutil.parser import isoparse
            
            opts = WriteOptions(
                metadata=m.get("metadata", {}),
                tenant_id=tenant_id,
            )
            if m.get("valid_from"):
                opts.valid_from = isoparse(m["valid_from"])
            
            store_entry.backend.write(m["content"], opts)
            synced_memories += 1
            
        store_entry.backend.flush()

        return {
            "status": "success",
            "synced_types": synced_types,
            "synced_memories": synced_memories,
        }
