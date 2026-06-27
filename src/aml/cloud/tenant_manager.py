"""
GRAFOMEM tenant manager — provision, configure, and rotate tenants.

Manages tenant lifecycle backed by PostgreSQL via psycopg (v3).  Each tenant
gets a unique ``gfm_``-prefixed API key, a plan with rate/storage limits, and
a row in the ``tenants`` table.  The manager is instantiated once per process
and shared via ``app.state.tenant_manager``.

Plan tiers mirror the cloud pricing page:
  starter     100 000 memories · 3 stores · 60 rpm
  pro       1 000 000 memories · 50 stores · 600 rpm
  enterprise  unlimited everything
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aml.server.scopes import ROLE_SCOPES, validate_scopes

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.tenants")


# ============================================================================
# Plan limits
# ============================================================================

# Sentinel for "unlimited" — any comparison with _INF returns True.
_INF = 2**63


@dataclass(slots=True)
class TenantLimits:
    """Per-plan resource ceilings."""
    max_memories: int
    max_stores: int
    max_requests_per_minute: int


PLAN_LIMITS: dict[str, TenantLimits] = {
    "starter": TenantLimits(
        max_memories=100_000,
        max_stores=3,
        max_requests_per_minute=60,
    ),
    "pro": TenantLimits(
        max_memories=1_000_000,
        max_stores=50,
        max_requests_per_minute=600,
    ),
    "enterprise": TenantLimits(
        max_memories=_INF,
        max_stores=_INF,
        max_requests_per_minute=_INF,
    ),
}

VALID_PLANS = frozenset(PLAN_LIMITS)


# ============================================================================
# Core data types
# ============================================================================

@dataclass(slots=True)
class TenantInfo:
    """Public-facing snapshot of a provisioned tenant."""
    id: str
    name: str
    api_key: str
    plan: str
    created_at: datetime
    limits: TenantLimits
    role: str = "admin"
    home_region: str = "global"


# ============================================================================
# TenantManager
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT        PRIMARY KEY,
    name        TEXT        NOT NULL,
    api_key     TEXT        NOT NULL UNIQUE,
    plan        TEXT        NOT NULL DEFAULT 'starter',
    email       TEXT        UNIQUE,
    supabase_uid TEXT       UNIQUE,
    password_hash TEXT,
    status      TEXT        NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    home_region TEXT        DEFAULT 'global'
);

CREATE TABLE IF NOT EXISTS tenant_api_keys (
    key_id      TEXT        PRIMARY KEY,
    tenant_id   TEXT        NOT NULL,
    api_key     TEXT        NOT NULL UNIQUE,
    name        TEXT        NOT NULL,
    role        TEXT        NOT NULL DEFAULT 'admin',
    scopes      TEXT[]      DEFAULT '{}',
    allowed_stores TEXT[]   DEFAULT '{}',
    expires_at  TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    ip_allowlist TEXT[]     DEFAULT '{}',
    is_service_account BOOLEAN DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tenant_api_keys_key ON tenant_api_keys (api_key);
CREATE INDEX IF NOT EXISTS idx_tenant_api_keys_tenant ON tenant_api_keys (tenant_id);
"""


def _generate_api_key(role: str = "admin", is_service_account: bool = False) -> str:
    """Return a prefixed API key with 48 hex chars of entropy."""
    if is_service_account:
        prefix = "gfm_sa_"
    elif role == "read_only":
        prefix = "gfm_ro_"
    else:
        prefix = "gfm_"
    return f"{prefix}{secrets.token_hex(24)}"


class TenantManager:
    """Manages tenant provisioning, API key generation, and configuration.

    All database access uses **psycopg v3 (sync)** — the same driver
    already in use by the Postgres memory backend.

    Parameters
    ----------
    db_url : str
        A PostgreSQL connection URI, e.g.
        ``postgresql://grafomem:dev@localhost:5432/grafomem``.
    """

    def __init__(self, db_url: str, pool=None) -> None:
        self._db_url = db_url
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        self._pool = pool

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        """Return an open connection, creating one lazily."""
        if self._pool is not None:
            return self._pool.getconn()
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        """Close the underlying database connection."""
        if self._pool is not None:
            self._conn = None
            return
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create the ``tenants`` table if it does not exist."""
        conn = self._get_conn()
        conn.execute(_SCHEMA_SQL)

        # Migrate existing API keys to tenant_api_keys
        conn.execute("""
            INSERT INTO tenant_api_keys (key_id, tenant_id, api_key, name, role, created_at)
            SELECT gen_random_uuid()::text, id, api_key, 'Default Admin Key', 'admin', created_at
            FROM tenants
            ON CONFLICT (api_key) DO NOTHING;
        """)
        
        # Add home_region column
        conn.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS home_region TEXT DEFAULT 'global';")
        
        logger.info("Tenant schema ensured")

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_tenant(self, name: str, plan: str = "starter", home_region: str = "global") -> TenantInfo:
        """Provision a new tenant and return its :class:`TenantInfo`.

        Parameters
        ----------
        name : str
            Human-readable tenant name (e.g. ``"Acme Corp"``).
        plan : str
            One of ``starter``, ``pro``, ``enterprise``.

        Returns
        -------
        TenantInfo
            Includes the freshly-generated ``gfm_``-prefixed API key.

        Raises
        ------
        ValueError
            If *plan* is not a recognised tier.
        """
        if plan not in VALID_PLANS:
            raise ValueError(
                f"Unknown plan {plan!r}. Valid plans: {sorted(VALID_PLANS)}"
            )

        tenant_id = uuid.uuid4().hex
        api_key = _generate_api_key()
        now = datetime.now(tz=timezone.utc)

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO tenants (id, name, api_key, plan, created_at, home_region) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (tenant_id, name, api_key, plan, now, home_region),
        )
        conn.execute(
            "INSERT INTO tenant_api_keys (key_id, tenant_id, api_key, name, role, scopes, created_at) "
            "VALUES (gen_random_uuid()::text, %s, %s, 'Default Admin Key', 'admin', %s, %s)",
            (tenant_id, api_key, ["*"], now),
        )
        logger.info("Tenant created: %s (%s, plan=%s)", tenant_id, name, plan)

        return TenantInfo(
            id=tenant_id,
            name=name,
            api_key=api_key,
            plan=plan,
            created_at=now,
            limits=PLAN_LIMITS[plan],
            home_region=home_region,
        )

    def get_tenant(self, tenant_id: str) -> TenantInfo | None:
        """Look up a tenant by ID.  Returns ``None`` if not found."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, name, api_key, plan, created_at, home_region FROM tenants WHERE id = %s",
            (tenant_id,),
        ).fetchone()
        return self._row_to_info(row) if row else None

    def get_tenant_by_key(self, api_key: str) -> TenantInfo | None:
        """Look up a tenant by its API key.  Returns ``None`` if not found."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT t.id, t.name, k.api_key, t.plan, t.created_at, k.role "
            "FROM tenants t "
            "JOIN tenant_api_keys k ON t.id = k.tenant_id "
            "WHERE k.api_key = %s",
            (api_key,),
        ).fetchone()
        
        if row:
            plan = row["plan"]
            return TenantInfo(
                id=row["id"],
                name=row["name"],
                api_key=row["api_key"],
                plan=plan,
                created_at=row["created_at"],
                limits=PLAN_LIMITS.get(plan, PLAN_LIMITS["starter"]),
                role=row["role"]
            )
            
        # Fallback to legacy column if not migrated
        row = conn.execute(
            "SELECT id, name, api_key, plan, created_at, home_region FROM tenants WHERE api_key = %s",
            (api_key,),
        ).fetchone()
        return self._row_to_info(row) if row else None

    def list_tenants(self) -> list[TenantInfo]:
        """Return every provisioned tenant, ordered by creation time."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, name, api_key, plan, created_at, home_region FROM tenants "
            "ORDER BY created_at",
        ).fetchall()
        return [self._row_to_info(r) for r in rows]

    def create_api_key(
        self, tenant_id: str, name: str, role: str = "admin",
        scopes: list[str] | None = None,
        allowed_stores: list[str] | None = None,
        expires_at: datetime | None = None,
        ip_allowlist: list[str] | None = None,
        is_service_account: bool = False,
    ) -> dict:
        """Create a new scoped API key for a tenant."""
        if role not in ("admin", "agent", "read_only"):
            raise ValueError(f"Invalid role: {role}")

        # Resolve scopes: explicit list wins, otherwise derive from role
        if scopes is not None:
            resolved_scopes = validate_scopes(scopes)
        else:
            resolved_scopes = list(ROLE_SCOPES.get(role, ROLE_SCOPES["admin"]))

        new_key = _generate_api_key(role=role, is_service_account=is_service_account)
        key_id = uuid.uuid4().hex
        now = datetime.now(tz=timezone.utc)
        conn = self._get_conn()

        # Verify tenant exists
        if not conn.execute("SELECT id FROM tenants WHERE id = %s", (tenant_id,)).fetchone():
            raise KeyError(f"Tenant {tenant_id!r} not found")

        conn.execute(
            "INSERT INTO tenant_api_keys "
            "(key_id, tenant_id, api_key, name, role, scopes, allowed_stores, "
            " expires_at, ip_allowlist, is_service_account, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                key_id, tenant_id, new_key, name, role,
                resolved_scopes,
                allowed_stores or [],
                expires_at,
                ip_allowlist or [],
                is_service_account,
                now,
            ),
        )
        logger.info("API key created for tenant %s (role=%s)", tenant_id, role)
        return {
            "api_key": new_key,
            "key_id": key_id,
            "role": role,
            "scopes": resolved_scopes,
            "expires_at": expires_at.isoformat() if expires_at else None,
        }

    def revoke_key(self, api_key: str) -> str:
        """Revoke a specific API key. Returns the deleted key value (for cache invalidation)."""
        conn = self._get_conn()
        cur = conn.execute("DELETE FROM tenant_api_keys WHERE api_key = %s RETURNING api_key", (api_key,))
        row = cur.fetchone()
        if not row:
            raise KeyError("API key not found")
        logger.info("API key revoked")
        return row[0] if isinstance(row, tuple) else row["api_key"]

    def revoke_key_by_id(self, key_id: str, tenant_id: str) -> str:
        """Revoke an API key by key_id (for portal/admin use). Returns the deleted api_key value."""
        conn = self._get_conn()
        cur = conn.execute(
            "DELETE FROM tenant_api_keys WHERE key_id = %s AND tenant_id = %s RETURNING api_key",
            (key_id, tenant_id),
        )
        row = cur.fetchone()
        if not row:
            raise KeyError(f"Key '{key_id}' not found for tenant")
        logger.info("API key revoked by key_id=%s", key_id)
        return row[0] if isinstance(row, tuple) else row["api_key"]

    def list_api_keys(self, tenant_id: str) -> list[dict[str, Any]]:
        """Return all API keys for a tenant (without the raw key value)."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT key_id, name, role, scopes, created_at, "
            "       last_used_at, expires_at "
            "FROM tenant_api_keys WHERE tenant_id = %s "
            "ORDER BY created_at",
            (tenant_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_key_scopes(self, key_id: str, scopes: list[str]) -> None:
        """Validate and update the scopes on an existing API key."""
        resolved = validate_scopes(scopes)
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE tenant_api_keys SET scopes = %s WHERE key_id = %s",
            (resolved, key_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"API key {key_id!r} not found")
        logger.info("Scopes updated for key %s", key_id)

    def update_plan(self, tenant_id: str, plan: str) -> TenantInfo:
        """Change a tenant's plan tier.

        Raises
        ------
        ValueError
            If *plan* is not recognised.
        KeyError
            If no tenant with *tenant_id* exists.
        """
        if plan not in VALID_PLANS:
            raise ValueError(
                f"Unknown plan {plan!r}. Valid plans: {sorted(VALID_PLANS)}"
            )
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE tenants SET plan = %s WHERE id = %s", (plan, tenant_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Tenant {tenant_id!r} not found")
        logger.info("Plan updated for tenant %s → %s", tenant_id, plan)

        info = self.get_tenant(tenant_id)
        assert info is not None  # we just updated it
        return info

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_info(row: dict[str, Any]) -> TenantInfo:
        """Convert a database row dict into a :class:`TenantInfo`."""
        plan = row["plan"]
        return TenantInfo(
            id=row["id"],
            name=row["name"],
            api_key=row.get("api_key", ""),
            plan=plan,
            created_at=row["created_at"],
            limits=PLAN_LIMITS.get(plan, PLAN_LIMITS["starter"]),
            role=row.get("role", "admin"),
            home_region=row.get("home_region", "global"),
        )

    # ------------------------------------------------------------------
    # Member management (Sprint 22)
    # ------------------------------------------------------------------

    def ensure_members_schema(self) -> None:
        """Create the ``tenant_members`` table if it does not exist."""
        conn = self._get_conn()
        conn.execute(_MEMBERS_SCHEMA_SQL)
        logger.info("Tenant members schema ensured")

    def invite_member(
        self,
        tenant_id: str,
        email: str,
        role: str = "member",
        invited_by: str = "",
    ) -> dict[str, Any]:
        """Invite a team member to a tenant.

        Returns the created member record.
        """
        if role not in VALID_ROLES:
            raise ValueError(
                f"Unknown role {role!r}. Valid roles: {sorted(VALID_ROLES)}"
            )

        member_id = uuid.uuid4().hex[:24]
        now = datetime.now(tz=timezone.utc)

        conn = self._get_conn()
        conn.execute(
            "INSERT INTO tenant_members "
            "(member_id, tenant_id, email, role, invited_by, joined_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, email) DO UPDATE SET role = %s",
            (member_id, tenant_id, email, role, invited_by, now, role),
        )
        logger.info(
            "Member invited: %s → tenant %s (role=%s)",
            email, tenant_id, role,
        )
        return {
            "member_id": member_id,
            "tenant_id": tenant_id,
            "email": email,
            "role": role,
            "invited_by": invited_by,
            "joined_at": now.isoformat(),
        }

    def list_members(self, tenant_id: str) -> list[dict[str, Any]]:
        """List all members of a tenant."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT member_id, tenant_id, email, role, invited_by, joined_at "
            "FROM tenant_members WHERE tenant_id = %s ORDER BY joined_at",
            (tenant_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_member_role(
        self,
        tenant_id: str,
        member_id: str,
        role: str,
    ) -> bool:
        """Change a member's role. Returns True if updated."""
        if role not in VALID_ROLES:
            raise ValueError(f"Unknown role {role!r}")
        conn = self._get_conn()
        result = conn.execute(
            "UPDATE tenant_members SET role = %s "
            "WHERE member_id = %s AND tenant_id = %s",
            (role, member_id, tenant_id),
        )
        return result.rowcount > 0

    def remove_member(self, tenant_id: str, member_id: str) -> bool:
        """Remove a member from a tenant. Returns True if removed."""
        conn = self._get_conn()
        result = conn.execute(
            "DELETE FROM tenant_members "
            "WHERE member_id = %s AND tenant_id = %s",
            (member_id, tenant_id),
        )
        return result.rowcount > 0

    def get_member_count(self, tenant_id: str) -> int:
        """Count members in a tenant."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM tenant_members WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()
        return row["cnt"] if row else 0


# ============================================================================
# Member roles and schema
# ============================================================================

VALID_ROLES = frozenset({"owner", "admin", "member", "viewer"})

_MEMBERS_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS tenant_members (
    member_id   TEXT        PRIMARY KEY,
    tenant_id   TEXT        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email       TEXT        NOT NULL,
    role        TEXT        NOT NULL DEFAULT 'member',
    invited_by  TEXT        NOT NULL DEFAULT '',
    joined_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(tenant_id, email)
);
CREATE INDEX IF NOT EXISTS idx_tm_tenant ON tenant_members(tenant_id);
"""
