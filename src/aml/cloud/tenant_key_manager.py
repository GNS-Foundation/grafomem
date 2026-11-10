import os
import base64
import asyncio
import logging
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken
import hashlib as _hashlib
from pathlib import Path as _Path

logger = logging.getLogger(__name__)

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

# =============================================================================
# 0019 — per-data-subject keys (GMP §7.7 DATA_SUBJECT; ADR-0019 F2/F4/F7)
#
# Hierarchy: KEK (GRAFOMEM_MASTER_KEY) → tenant DEK → RANDOM per-data-subject DEK,
# wrapped under the tenant DEK and stored in `data_subject_deks` on the key-store URL
# (the ledger role, F4). Random, not derived: a derived key cannot be destroyed on its
# own. Destroying the tenant DEK still makes every data-subject DEK under it unwrappable.
#
# Ciphertext under a data-subject key carries the `gfm.ds1:` prefix so a reader can
# pick the key from the row itself; a tenant-keyed row has no prefix (F7: rows written
# before the tenant opted in keep decrypting under the tenant key, and are deletion-only
# on erasure — the certificate says so per fact).
# =============================================================================

DS_PREFIX = "gfm.ds1:"


class DataSubjectKeyDestroyed(Exception):
    """The data subject's key was destroyed: its ciphertext is unrecoverable by design."""


class DataSubjectEncryptor:
    """Fernet under a data-subject DEK; prefixes ciphertext with DS_PREFIX."""

    def __init__(self, fernet: Fernet, fingerprint: str):
        self._fernet = fernet
        self.fingerprint = fingerprint

    def encrypt(self, plaintext: str) -> str:
        return DS_PREFIX + self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext.startswith(DS_PREFIX):
            raise ValueError("not a data-subject-keyed ciphertext")
        try:
            return self._fernet.decrypt(ciphertext[len(DS_PREFIX):].encode()).decode()
        except InvalidToken:
            raise ValueError("Decryption failures are strictly denied")


def key_fingerprint(raw_dek: bytes) -> str:
    """Stable public identifier of a DEK (never reveals it): SHA-256, first 32 hex."""
    return _hashlib.sha256(raw_dek).hexdigest()[:32]


def data_subject_fingerprint(tenant_id: str, data_subject_id: str) -> str:
    """What the ledger records instead of the data subject's identifier."""
    return _hashlib.sha256(f"{tenant_id}\x00{data_subject_id}".encode()).hexdigest()[:32]


def _ds_ddl() -> str:
    """The shipped migration is the single source of the key-store DDL (as 021 is for the
    ledger chain): self-host `ensure_schema()` executes the same file the cloud
    pre-deploy runner applies."""
    return (_Path(__file__).parent / "migrations" / "022_data_subject_keys.sql").read_text()


class DataSubjectKeyMixin:
    """Mixed into TenantKeyManager. Needs self._pool, self._kek, self._cache,
    self.get_encryptor(tenant_id)."""

    _ds_cache: dict
    _ds_opt_in = None  # Callable[[str], bool] | None — tenant opted in to data-subject keys?

    def configure_data_subject_keys(self, opt_in) -> None:
        """`opt_in(tenant_id) -> bool` decides whether NEW facts with a data_subject_id are
        keyed per data subject (F7, opt-in per tenant). Reads never consult it: a
        data-subject-keyed row is decrypted with its key whatever the flag says now."""
        self._ds_opt_in = opt_in

    def ensure_data_subject_schema(self) -> None:
        with self._pool.connection() as conn:
            conn.execute(_ds_ddl())

    # -- key lifecycle --------------------------------------------------------

    def _tenant_fernet(self, tenant_id: str) -> Fernet:
        return self.get_encryptor(tenant_id)._fernet  # raises TenantKeyDestroyed

    def _ds_row(self, conn, tenant_id: str, data_subject_id: str):
        return conn.execute(
            "SELECT wrapped_dek, key_fingerprint, destroyed_at FROM data_subject_deks "
            "WHERE tenant_id = %s AND data_subject_id = %s",
            (tenant_id, data_subject_id),
        ).fetchone()

    def get_data_subject_encryptor(self, tenant_id: str, data_subject_id: str,
                                   require: bool = False) -> Optional[DataSubjectEncryptor]:
        """The data subject's encryptor.

        require=False (a WRITE): returns None unless the tenant opted in; creates the
        key on first use. require=True (a READ of a `gfm.ds1:` row): the key must exist —
        the opt-in flag is irrelevant — and a destroyed key raises DataSubjectKeyDestroyed.
        """
        if not hasattr(self, "_ds_cache"):
            self._ds_cache = {}
        ck = (tenant_id, data_subject_id)
        if ck in self._ds_cache:
            return self._ds_cache[ck]
        if not require and not (self._ds_opt_in and self._ds_opt_in(tenant_id)):
            return None
        tenant_fernet = self._tenant_fernet(tenant_id)
        with self._pool.connection() as conn:
            row = self._ds_row(conn, tenant_id, data_subject_id)
            if row:
                wrapped, fp, destroyed_at = row
                if destroyed_at is not None:
                    raise DataSubjectKeyDestroyed(
                        f"data subject key {fp} destroyed; ciphertext is unrecoverable by design")
                raw = tenant_fernet.decrypt(wrapped.encode())
            else:
                if require:
                    raise DataSubjectKeyDestroyed("no key on record for this data subject")
                raw = Fernet.generate_key()
                fp = key_fingerprint(raw)
                conn.execute(
                    "INSERT INTO data_subject_deks (tenant_id, data_subject_id, wrapped_dek, key_fingerprint) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (tenant_id, data_subject_id) DO NOTHING",
                    (tenant_id, data_subject_id, tenant_fernet.encrypt(raw).decode(), fp),
                )
                # a concurrent first write may have won: re-read so both processes use ONE key
                row = self._ds_row(conn, tenant_id, data_subject_id)
                wrapped, fp, destroyed_at = row
                if destroyed_at is not None:
                    raise DataSubjectKeyDestroyed(f"data subject key {fp} destroyed")
                raw = tenant_fernet.decrypt(wrapped.encode())
        enc = DataSubjectEncryptor(Fernet(raw), fp)
        self._ds_cache[ck] = enc
        return enc

    def data_subject_key_status(self, tenant_id: str, data_subject_id: str) -> tuple[str, Optional[str]]:
        """('absent' | 'active' | 'destroyed', fingerprint) — read from the key store, never
        the cache: this is what verification trusts."""
        with self._pool.connection() as conn:
            row = self._ds_row(conn, tenant_id, data_subject_id)
        if row is None:
            return "absent", None
        return ("destroyed" if row[2] is not None else "active"), row[1]

    def data_subject_key_status_by_fingerprint(self, tenant_id: str, fingerprint: str) -> tuple[str, Optional[str]]:
        """('absent' | 'active' | 'destroyed', fingerprint) looked up by the KEY fingerprint
        (what a certificate binds; the data subject's id is never in a certificate)."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT key_fingerprint, destroyed_at FROM data_subject_deks WHERE tenant_id = %s AND key_fingerprint = %s",
                (tenant_id, fingerprint),
            ).fetchone()
        if row is None:
            return "absent", None
        return ("destroyed" if row[1] is not None else "active"), row[0]

    def redestroy_data_subject_key_by_fingerprint(self, tenant_id: str, fingerprint: str) -> bool:
        """Restore-scrub (F4): a restore may bring a destroyed key back. Re-destroy it by
        fingerprint. Returns True when a live key was found and destroyed again."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE data_subject_deks SET wrapped_dek = 'DESTROYED', destroyed_at = now() "
                "WHERE tenant_id = %s AND key_fingerprint = %s AND destroyed_at IS NULL "
                "RETURNING data_subject_id",
                (tenant_id, fingerprint),
            ).fetchone()
            if row:
                self._drop_cached(f"ds:{tenant_id}:{row[0]}")
                conn.execute("SELECT pg_notify('dek_invalidations', %s)", (f"ds:{tenant_id}:{row[0]}",))
        return row is not None

    def destroy_data_subject_key(self, tenant_id: str, data_subject_id: str) -> Optional[str]:
        """Crypto-erasure of one data subject: overwrite the wrapped DEK and tombstone the
        row (the fingerprint stays, so a certificate can be checked against it). Returns
        the destroyed key's fingerprint, or None when the subject never had a key (F7:
        its facts were tenant-keyed — deletion only). Idempotent."""
        if hasattr(self, "_ds_cache"):
            self._ds_cache.pop((tenant_id, data_subject_id), None)
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE data_subject_deks SET wrapped_dek = 'DESTROYED', destroyed_at = COALESCE(destroyed_at, now()) "
                "WHERE tenant_id = %s AND data_subject_id = %s RETURNING key_fingerprint",
                (tenant_id, data_subject_id),
            ).fetchone()
            conn.execute("SELECT pg_notify('dek_invalidations', %s)",
                         (f"ds:{tenant_id}:{data_subject_id}",))
        return row[0] if row else None

    def _drop_cached(self, payload: str) -> None:
        """Invalidation listener hook: 'ds:<tenant>:<subject>' or a bare tenant id."""
        if payload.startswith("ds:"):
            _, t, s = payload.split(":", 2)
            if hasattr(self, "_ds_cache"):
                self._ds_cache.pop((t, s), None)
        else:
            self._cache.pop(payload, None)
            if hasattr(self, "_ds_cache"):
                for k in [k for k in self._ds_cache if k[0] == payload]:
                    self._ds_cache.pop(k, None)



class TenantKeyManager(DataSubjectKeyMixin):
    """
    Manages per-tenant random Data Encryption Keys (DEKs) wrapped under a master KEK.
    Stores wrapped DEKs in `tenant_deks` on `key_store_url`. CORRECTION (0019): this
    docstring used to say "an external key store DB, separate from the primary data DB";
    the app passed the primary db_url (a restore brought destroyed keys back). Since 0019
    the app passes the ledger URL (F4) — logical separation, same instance today.
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

        self._pool = ConnectionPool(
            self._key_store_url, min_size=1, max_size=10, open=open,
            timeout=float(os.environ.get("GRAFOMEM_DB_POOL_TIMEOUT", "10")),
            kwargs={"connect_timeout": int(os.environ.get("GRAFOMEM_DB_CONNECT_TIMEOUT", "5"))},
        )

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
            
            # Broadcast to all other nodes to drop their DEK caches
            conn.execute("SELECT pg_notify('dek_invalidations', %s)", (tenant_id,))

        return tenant_id

    async def start_invalidation_listener(self):
        """
        Runs continuously in the background to receive DEK invalidations from other nodes.
        Uses an AsyncConnection to avoid blocking the event loop.
        """
        import psycopg
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    self._key_store_url, autocommit=True,
                    connect_timeout=int(os.environ.get("GRAFOMEM_DB_CONNECT_TIMEOUT", "5")),
                ) as aconn:
                    await aconn.execute("LISTEN dek_invalidations")
                    logger.info("Listening for cross-node DEK invalidations.")
                    async for notify in aconn.notifies():
                        payload = notify.payload
                        self._drop_cached(payload)  # tenant id, or 'ds:<tenant>:<subject>' (0019)
                        logger.info(f"Node dropped DEK cache for {payload} via notification")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"DEK invalidation listener error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
