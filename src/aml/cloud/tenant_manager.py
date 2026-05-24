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


# ============================================================================
# TenantManager
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT        PRIMARY KEY,
    name        TEXT        NOT NULL,
    api_key     TEXT        NOT NULL UNIQUE,
    plan        TEXT        NOT NULL DEFAULT 'starter',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tenants_api_key ON tenants (api_key);
"""


def _generate_api_key() -> str:
    """Return a ``gfm_``-prefixed API key with 48 hex chars of entropy."""
    return f"gfm_{secrets.token_hex(24)}"


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

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url
        self._conn: psycopg.Connection[dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        """Return an open connection, creating one lazily."""
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        """Close the underlying database connection."""
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create the ``tenants`` table if it does not exist."""
        conn = self._get_conn()
        conn.execute(_SCHEMA_SQL)
        logger.info("Tenant schema ensured")

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_tenant(self, name: str, plan: str = "starter") -> TenantInfo:
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
            "INSERT INTO tenants (id, name, api_key, plan, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (tenant_id, name, api_key, plan, now),
        )
        logger.info("Tenant created: %s (%s, plan=%s)", tenant_id, name, plan)

        return TenantInfo(
            id=tenant_id,
            name=name,
            api_key=api_key,
            plan=plan,
            created_at=now,
            limits=PLAN_LIMITS[plan],
        )

    def get_tenant(self, tenant_id: str) -> TenantInfo | None:
        """Look up a tenant by ID.  Returns ``None`` if not found."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, name, api_key, plan, created_at FROM tenants WHERE id = %s",
            (tenant_id,),
        ).fetchone()
        return self._row_to_info(row) if row else None

    def get_tenant_by_key(self, api_key: str) -> TenantInfo | None:
        """Look up a tenant by its API key.  Returns ``None`` if not found."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, name, api_key, plan, created_at FROM tenants WHERE api_key = %s",
            (api_key,),
        ).fetchone()
        return self._row_to_info(row) if row else None

    def list_tenants(self) -> list[TenantInfo]:
        """Return every provisioned tenant, ordered by creation time."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT id, name, api_key, plan, created_at FROM tenants "
            "ORDER BY created_at",
        ).fetchall()
        return [self._row_to_info(r) for r in rows]

    def revoke_key(self, tenant_id: str) -> str:
        """Revoke the current API key and generate a replacement.

        Returns
        -------
        str
            The newly-generated API key.

        Raises
        ------
        KeyError
            If no tenant with *tenant_id* exists.
        """
        new_key = _generate_api_key()
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE tenants SET api_key = %s WHERE id = %s",
            (new_key, tenant_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Tenant {tenant_id!r} not found")
        logger.info("API key rotated for tenant %s", tenant_id)
        return new_key

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
            api_key=row["api_key"],
            plan=plan,
            created_at=row["created_at"],
            limits=PLAN_LIMITS.get(plan, PLAN_LIMITS["starter"]),
        )
