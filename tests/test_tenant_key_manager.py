import os
import asyncio
import pytest
from aml.cloud.tenant_key_manager import TenantKeyManager

@pytest.fixture
def temp_db_url():
    url = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")
    return url

@pytest.mark.asyncio
async def test_cross_node_dek_invalidation(temp_db_url):
    master_key = os.urandom(32).hex()
    
    # Simulate Node A (the node doing the destroying)
    tkm_a = TenantKeyManager(master_key, temp_db_url)
    tkm_a.ensure_schema()
    
    # Simulate Node B (the node listening)
    tkm_b = TenantKeyManager(master_key, temp_db_url)
    
    # Start Node B's listener
    listener_task = asyncio.create_task(tkm_b.start_invalidation_listener())
    
    try:
        # Give listener a tiny moment to connect and execute LISTEN
        await asyncio.sleep(0.5)
        
        tenant_id = "test_tenant_dek_invalidate"
        
        # 1. Fetch encryptor on Node B to populate its cache
        enc_b = tkm_b.get_encryptor(tenant_id)
        assert tenant_id in tkm_b._cache
        
        # 2. Node A destroys the key (hard deletes from DB, tombstones, and sends NOTIFY)
        tkm_a.destroy_tenant_key(tenant_id)
        
        # 3. Wait a moment for Postgres NOTIFY to propagate over the network to Node B
        await asyncio.sleep(1.0)
        
        # 4. Verify Node B's cache was invalidated synchronously by the background task
        assert tenant_id not in tkm_b._cache
        
    finally:
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass
