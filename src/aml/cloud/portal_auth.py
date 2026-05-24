"""
GRAFOMEM portal auth — email/password authentication with JWT sessions.

Provides signup, login, and JWT verification for the self-service tenant
portal.  Passwords are hashed with bcrypt; sessions are stateless JWTs
(24 h expiry).  Backed by the existing ``tenants`` table extended with
``email`` and ``password_hash`` columns.

The ``bcrypt`` and ``PyJWT`` packages are soft-imported so the module
loads gracefully when they are missing (``cloud`` extra not installed).
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.portal_auth")

# Soft-import bcrypt and jwt
try:
    import bcrypt as _bcrypt
except ImportError:
    _bcrypt = None  # type: ignore[assignment]

try:
    import jwt as _jwt
except ImportError:
    _jwt = None  # type: ignore[assignment]


# ============================================================================
# Schema extension
# ============================================================================

_AUTH_COLUMNS_SQL = """\
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenants' AND column_name = 'email'
    ) THEN
        ALTER TABLE tenants ADD COLUMN email TEXT UNIQUE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenants' AND column_name = 'password_hash'
    ) THEN
        ALTER TABLE tenants ADD COLUMN password_hash TEXT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenants' AND column_name = 'stripe_customer_id'
    ) THEN
        ALTER TABLE tenants ADD COLUMN stripe_customer_id TEXT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenants' AND column_name = 'status'
    ) THEN
        ALTER TABLE tenants ADD COLUMN status TEXT DEFAULT 'active';
    END IF;
END $$;
"""


def _generate_api_key() -> str:
    """Return a ``gfm_``-prefixed API key with 48 hex chars of entropy."""
    return f"gfm_{secrets.token_hex(24)}"


# ============================================================================
# PortalAuth
# ============================================================================

class PortalAuth:
    """Email/password authentication and JWT session management.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    secret_key : str | None
        Secret for signing JWTs.  Falls back to ``GRAFOMEM_PORTAL_SECRET``
        env var, or generates a random key (ephemeral — tokens won't survive
        server restarts).
    """

    JWT_ALGORITHM = "HS256"
    JWT_EXPIRY_HOURS = 24

    def __init__(self, db_url: str, secret_key: str | None = None) -> None:
        self._db_url = db_url
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        self._secret = (
            secret_key
            or os.environ.get("GRAFOMEM_PORTAL_SECRET")
            or secrets.token_hex(32)
        )

        if _bcrypt is None:
            logger.warning("bcrypt not installed — portal signup/login disabled")
        if _jwt is None:
            logger.warning("PyJWT not installed — portal sessions disabled")

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Add auth columns (email, password_hash, etc.) to ``tenants``."""
        conn = self._get_conn()
        conn.execute(_AUTH_COLUMNS_SQL)
        logger.info("Portal auth columns ensured on tenants table")

    # ------------------------------------------------------------------
    # Signup
    # ------------------------------------------------------------------

    def signup(
        self,
        name: str,
        email: str,
        password: str,
        plan: str = "starter",
    ) -> tuple[dict, str]:
        """Create a new tenant with email/password credentials.

        Parameters
        ----------
        name : str
            Human-readable tenant / organization name.
        email : str
            Unique email address for login.
        password : str
            Plaintext password (will be bcrypt-hashed).
        plan : str
            Initial plan tier.

        Returns
        -------
        tuple[dict, str]
            ``(tenant_info_dict, jwt_token)`` — the dict contains keys:
            ``tenant_id``, ``name``, ``email``, ``api_key``, ``plan``.

        Raises
        ------
        ValueError
            If the email is already registered or password is too short.
        """
        if _bcrypt is None:
            raise RuntimeError("bcrypt not installed — cannot signup")

        email = email.strip().lower()
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")

        conn = self._get_conn()

        # Check email uniqueness
        existing = conn.execute(
            "SELECT id FROM tenants WHERE email = %s", (email,),
        ).fetchone()
        if existing:
            raise ValueError(f"Email {email!r} is already registered")

        # Hash password
        pw_hash = _bcrypt.hashpw(
            password.encode("utf-8"), _bcrypt.gensalt(),
        ).decode("ascii")

        # Create tenant record
        tenant_id = uuid.uuid4().hex
        api_key = _generate_api_key()
        now = datetime.now(tz=timezone.utc)

        conn.execute(
            "INSERT INTO tenants (id, name, api_key, plan, created_at, "
            "  email, password_hash, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')",
            (tenant_id, name, api_key, plan, now, email, pw_hash),
        )

        logger.info("Tenant signed up: %s (%s, %s)", tenant_id, name, email)

        info = {
            "tenant_id": tenant_id,
            "name": name,
            "email": email,
            "api_key": api_key,
            "plan": plan,
        }
        token = self._issue_jwt(tenant_id, email)
        return info, token

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def login(self, email: str, password: str) -> tuple[dict, str] | None:
        """Authenticate with email and password.

        Returns
        -------
        tuple[dict, str] | None
            ``(tenant_info_dict, jwt_token)`` on success, ``None`` on failure.
        """
        if _bcrypt is None:
            raise RuntimeError("bcrypt not installed — cannot login")

        email = email.strip().lower()
        conn = self._get_conn()

        row = conn.execute(
            "SELECT id, name, api_key, plan, email, password_hash "
            "FROM tenants WHERE email = %s",
            (email,),
        ).fetchone()

        if not row or not row.get("password_hash"):
            return None

        # Verify password
        if not _bcrypt.checkpw(
            password.encode("utf-8"),
            row["password_hash"].encode("ascii"),
        ):
            return None

        info = {
            "tenant_id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "api_key": row["api_key"],
            "plan": row["plan"],
        }
        token = self._issue_jwt(row["id"], row["email"])
        return info, token

    # ------------------------------------------------------------------
    # JWT
    # ------------------------------------------------------------------

    def verify_token(self, token: str) -> dict | None:
        """Verify a JWT and return tenant info, or ``None`` if invalid.

        Returns
        -------
        dict | None
            Keys: ``tenant_id``, ``name``, ``email``, ``api_key``, ``plan``.
        """
        if _jwt is None:
            return None

        try:
            payload = _jwt.decode(
                token, self._secret, algorithms=[self.JWT_ALGORITHM],
            )
        except _jwt.ExpiredSignatureError:
            logger.debug("JWT expired")
            return None
        except _jwt.InvalidTokenError:
            logger.debug("Invalid JWT")
            return None

        tenant_id = payload.get("sub")
        if not tenant_id:
            return None

        # Fetch current tenant info from DB
        conn = self._get_conn()
        row = conn.execute(
            "SELECT id, name, api_key, plan, email "
            "FROM tenants WHERE id = %s",
            (tenant_id,),
        ).fetchone()

        if not row:
            return None

        return {
            "tenant_id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "api_key": row["api_key"],
            "plan": row["plan"],
        }

    def _issue_jwt(self, tenant_id: str, email: str) -> str:
        """Create a signed JWT with 24 h expiry."""
        if _jwt is None:
            raise RuntimeError("PyJWT not installed — cannot issue token")

        now = datetime.now(tz=timezone.utc)
        payload = {
            "sub": tenant_id,
            "email": email,
            "iat": now,
            "exp": now + timedelta(hours=self.JWT_EXPIRY_HOURS),
        }
        return _jwt.encode(payload, self._secret, algorithm=self.JWT_ALGORITHM)
