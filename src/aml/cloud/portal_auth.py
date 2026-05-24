"""
GRAFOMEM portal auth — Supabase Auth integration with legacy fallback.

Verifies Supabase JWTs (from the frontend supabase-js client) and
auto-provisions tenants in the ``tenants`` table on first login.

Also retains legacy email/password signup/login for backward compatibility.
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

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenants' AND column_name = 'supabase_uid'
    ) THEN
        ALTER TABLE tenants ADD COLUMN supabase_uid TEXT UNIQUE;
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
    """Supabase Auth + legacy email/password authentication.

    Supports two modes:
    1. **Supabase** (primary) — verifies JWTs issued by Supabase Auth using
       the project's JWT secret.  Auto-provisions tenants on first login.
    2. **Legacy** — email/password with bcrypt + self-issued JWTs (kept for
       backward compatibility and existing sessions).
    """

    JWT_ALGORITHM = "HS256"
    JWT_EXPIRY_HOURS = 24

    def __init__(self, db_url: str, secret_key: str | None = None) -> None:
        self._db_url = db_url
        self._conn: psycopg.Connection[dict[str, Any]] | None = None

        # Legacy portal JWT secret
        self._secret = (
            secret_key
            or os.environ.get("GRAFOMEM_PORTAL_SECRET")
            or secrets.token_hex(32)
        )

        # Supabase config — verify tokens via API call
        self._supabase_url = os.environ.get(
            "SUPABASE_URL", "https://wlhmlgnqebqnkhyoamaf.supabase.co"
        )
        self._supabase_service_key = os.environ.get(
            "SUPABASE_SERVICE_ROLE_KEY", ""
        )

        if _bcrypt is None:
            logger.warning("bcrypt not installed — legacy signup/login disabled")
        if _jwt is None:
            logger.warning("PyJWT not installed — portal sessions disabled")
        if self._supabase_url:
            logger.info("Supabase Auth API verification enabled (%s)", self._supabase_url)

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
        """Add auth columns (email, password_hash, supabase_uid) to ``tenants``."""
        conn = self._get_conn()
        conn.execute(_AUTH_COLUMNS_SQL)
        logger.info("Portal auth columns ensured on tenants table")

    # ------------------------------------------------------------------
    # Supabase JWT verification
    # ------------------------------------------------------------------

    def verify_supabase_token(self, token: str) -> dict | None:
        """Verify a Supabase access token via the Supabase Auth API.

        Calls ``GET /auth/v1/user`` with the token to validate it
        server-side.  No JWT secret required — Supabase handles
        verification internally.

        Returns
        -------
        dict | None
            Keys: ``sub`` (Supabase user UUID), ``email``,
            ``user_metadata`` (dict with name, plan, etc.).
        """
        if not self._supabase_url:
            return None

        import urllib.request
        import json

        url = f"{self._supabase_url}/auth/v1/user"
        headers = {
            "Authorization": f"Bearer {token}",
            "apikey": self._supabase_service_key or token,
        }

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status != 200:
                    logger.debug("Supabase API returned %s", resp.status)
                    return None
                user = json.loads(resp.read())
        except Exception as exc:
            logger.debug("Supabase API verification failed: %s", exc)
            return None

        sub = user.get("id")
        if not sub:
            return None

        return {
            "sub": sub,
            "email": user.get("email", ""),
            "user_metadata": user.get("user_metadata", {}),
        }

    # ------------------------------------------------------------------
    # Tenant auto-provisioning (for Supabase users)
    # ------------------------------------------------------------------

    def ensure_tenant(
        self,
        supabase_uid: str,
        email: str,
        name: str = "",
        plan: str = "starter",
    ) -> dict:
        """Find or create a tenant linked to a Supabase user.

        Called after Supabase JWT verification.  If a tenant with the given
        ``supabase_uid`` already exists, return it.  Otherwise create a new
        tenant record.

        Returns
        -------
        dict
            Keys: ``tenant_id``, ``name``, ``email``, ``api_key``, ``plan``.
        """
        conn = self._get_conn()

        # Look up by supabase_uid first
        row = conn.execute(
            "SELECT id, name, api_key, plan, email "
            "FROM tenants WHERE supabase_uid = %s",
            (supabase_uid,),
        ).fetchone()

        if row:
            return {
                "tenant_id": row["id"],
                "name": row["name"],
                "email": row["email"],
                "api_key": row["api_key"],
                "plan": row["plan"],
            }

        # Check if there's a tenant with this email (legacy account migration)
        row = conn.execute(
            "SELECT id, name, api_key, plan, email "
            "FROM tenants WHERE email = %s",
            (email,),
        ).fetchone()

        if row:
            # Link existing tenant to Supabase UID
            conn.execute(
                "UPDATE tenants SET supabase_uid = %s WHERE id = %s",
                (supabase_uid, row["id"]),
            )
            logger.info(
                "Linked existing tenant %s to Supabase UID %s",
                row["id"], supabase_uid,
            )
            return {
                "tenant_id": row["id"],
                "name": row["name"],
                "email": row["email"],
                "api_key": row["api_key"],
                "plan": row["plan"],
            }

        # Create a new tenant
        tenant_id = uuid.uuid4().hex
        api_key = _generate_api_key()
        now = datetime.now(tz=timezone.utc)
        final_name = name or email.split("@")[0]

        conn.execute(
            "INSERT INTO tenants (id, name, api_key, plan, created_at, "
            "  email, supabase_uid, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'active')",
            (tenant_id, final_name, api_key, plan, now, email, supabase_uid),
        )

        logger.info(
            "Auto-provisioned tenant %s for Supabase user %s (%s)",
            tenant_id, supabase_uid, email,
        )

        return {
            "tenant_id": tenant_id,
            "name": final_name,
            "email": email,
            "api_key": api_key,
            "plan": plan,
        }

    # ------------------------------------------------------------------
    # Legacy signup
    # ------------------------------------------------------------------

    def signup(
        self,
        name: str,
        email: str,
        password: str,
        plan: str = "starter",
    ) -> tuple[dict, str]:
        """Create a new tenant with email/password credentials (legacy)."""
        if _bcrypt is None:
            raise RuntimeError("bcrypt not installed — cannot signup")

        email = email.strip().lower()
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")

        conn = self._get_conn()

        existing = conn.execute(
            "SELECT id FROM tenants WHERE email = %s", (email,),
        ).fetchone()
        if existing:
            raise ValueError(f"Email {email!r} is already registered")

        pw_hash = _bcrypt.hashpw(
            password.encode("utf-8"), _bcrypt.gensalt(),
        ).decode("ascii")

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
    # Legacy login
    # ------------------------------------------------------------------

    def login(self, email: str, password: str) -> tuple[dict, str] | None:
        """Authenticate with email and password (legacy)."""
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
    # JWT (legacy self-issued + Supabase verification)
    # ------------------------------------------------------------------

    def verify_token(self, token: str) -> dict | None:
        """Verify a token — tries Supabase JWT first, then legacy.

        Returns
        -------
        dict | None
            Keys: ``tenant_id``, ``name``, ``email``, ``api_key``, ``plan``.
        """
        # Try Supabase JWT first
        sb_info = self.verify_supabase_token(token)
        if sb_info:
            # Auto-provision / look up tenant
            tenant = self.ensure_tenant(
                supabase_uid=sb_info["sub"],
                email=sb_info["email"],
                name=sb_info["user_metadata"].get("name")
                    or sb_info["user_metadata"].get("full_name", ""),
                plan=sb_info["user_metadata"].get("plan", "starter"),
            )
            return tenant

        # Fall back to legacy JWT
        return self._verify_legacy_token(token)

    def _verify_legacy_token(self, token: str) -> dict | None:
        """Verify a legacy self-issued JWT."""
        if _jwt is None:
            return None

        try:
            payload = _jwt.decode(
                token, self._secret, algorithms=[self.JWT_ALGORITHM],
            )
        except _jwt.ExpiredSignatureError:
            logger.debug("Legacy JWT expired")
            return None
        except _jwt.InvalidTokenError:
            logger.debug("Invalid legacy JWT")
            return None

        tenant_id = payload.get("sub")
        if not tenant_id:
            return None

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
        """Create a signed legacy JWT with 24 h expiry."""
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
