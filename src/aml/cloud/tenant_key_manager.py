import os
import base64
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken

class TenantKeyDestroyed(Exception):
    """Raised when attempting to access a key for a tenant that has been crypto-erased."""
    pass

class FernetEncryptor:
    """Implements AtRestEncryption protocol around a single Fernet instance."""
    def __init__(self, fernet: Fernet):
        self._fernet = fernet

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            raise ValueError("Decryption failures are strictly denied")

class TenantKeyManager:
    """
    Manages per-tenant random Data Encryption Keys (DEKs) wrapped under a master KEK.
    Stores wrapped DEKs in an external key store DB, separate from the primary data DB.
    """
    def __init__(self, master_key_hex: str, key_store_url: str, open: bool = True):
        if not master_key_hex or len(master_key_hex) < 64:
            raise ValueError("GRAFOMEM_MASTER_KEY must be at least 32 bytes (64 hex chars)")
            
        # Use first 32 bytes of the hex decoded string as the KEK
        kek_bytes = bytes.fromhex(master_key_hex)[:32]
        self._kek = Fernet(base64.urlsafe_b64encode(kek_bytes))
        self._key_store_url = key_store_url
        self._cache: dict[str, FernetEncryptor] = {}
        
        try:
            import psycopg
            from psycopg_pool import ConnectionPool
        except ImportError as e:
            raise RuntimeError("TenantKeyManager requires psycopg and psycopg_pool") from e

        self._pool = ConnectionPool(self._key_store_url, min_size=1, max_size=10, open=open)

    def ensure_schema(self):
        with self._pool.connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tenant_deks (
                    tenant_id   TEXT PRIMARY KEY,
                    wrapped_dek TEXT NOT NULL,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                    destroyed_at TIMESTAMPTZ
                );
            """)

    def get_encryptor(self, tenant_id: str) -> FernetEncryptor:
        if tenant_id in self._cache:
            return self._cache[tenant_id]

        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT wrapped_dek, destroyed_at FROM tenant_deks WHERE tenant_id = %s",
                (tenant_id,)
            ).fetchone()

            if row:
                if row[1] is not None:
                    raise TenantKeyDestroyed(f"Tenant {tenant_id} has been crypto-erased.")
                wrapped_dek = row[0]
            else:
                # Generate new random DEK
                raw_dek = Fernet.generate_key()
                wrapped_dek = self._kek.encrypt(raw_dek).decode()
                conn.execute(
                    "INSERT INTO tenant_deks (tenant_id, wrapped_dek) VALUES (%s, %s)",
                    (tenant_id, wrapped_dek)
                )

        raw_dek = self._kek.decrypt(wrapped_dek.encode())
        encryptor = FernetEncryptor(Fernet(raw_dek))
        self._cache[tenant_id] = encryptor
        return encryptor

    def destroy_tenant_key(self, tenant_id: str) -> str:
        """
        Crypto-erasure: Hard-deletes the wrapped DEK from the key store.
        Returns the tenant_id if successful.
        """
        if tenant_id in self._cache:
            del self._cache[tenant_id]

        with self._pool.connection() as conn:
            res = conn.execute(
                "DELETE FROM tenant_deks WHERE tenant_id = %s RETURNING tenant_id",
                (tenant_id,)
            ).fetchone()

            # Record a tombstone so future get_encryptor calls know it was destroyed
            conn.execute(
                "INSERT INTO tenant_deks (tenant_id, wrapped_dek, destroyed_at) "
                "VALUES (%s, %s, now()) "
                "ON CONFLICT (tenant_id) DO UPDATE SET destroyed_at = now()",
                (tenant_id, "DESTROYED")
            )

        return tenant_id
