"""
GRAFOMEM SSO Provider — OpenID Connect + SAML 2.0 authentication for the Cloud Portal.

Supports OAuth2/OIDC flows for Google, Microsoft, GitHub, and generic OIDC
providers (Okta, Auth0, etc.), plus SAML 2.0 SP-initiated flows for enterprise
IdPs (Azure AD, Okta SAML, Ping Identity, etc.).  On successful authentication,
resolves the user's email to an existing tenant or creates a new one, then
issues a GRAFOMEM JWT.

Tables:
  - ``sso_configs``: per-tenant SSO provider configuration

Usage::

    provider = SSOProvider(db_url, portal_auth=pa, redirect_base="https://cloud.grafomem.com")
    auth_url = provider.initiate_flow("google", tenant_id=None)
    # ... user redirected to Google ...
    result = provider.handle_callback(code, state)
    # result = {"token": "...", "tenant_id": "...", "email": "..."}
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.sso")


# ============================================================================
# Well-known OIDC providers
# ============================================================================

PROVIDER_CONFIGS = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://www.googleapis.com/oauth2/v3/userinfo",
        "scopes": ["openid", "email", "profile"],
    },
    "microsoft": {
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "userinfo_url": "https://graph.microsoft.com/v1.0/me",
        "scopes": ["openid", "email", "profile"],
    },
    "github": {
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user",
        "scopes": ["read:user", "user:email"],
    },
}


# ============================================================================
# Data types
# ============================================================================

@dataclass(slots=True)
class SSOConfig:
    """SSO configuration for a provider."""
    config_id: str
    provider: str
    client_id: str
    client_secret: str
    issuer_url: str
    enabled: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(slots=True)
class SAMLConfig:
    """SAML 2.0 IdP configuration."""
    config_id: str
    idp_entity_id: str
    idp_sso_url: str
    idp_slo_url: str
    idp_x509_cert: str
    sp_entity_id: str
    attribute_mapping: dict = field(default_factory=lambda: {
        "email": "urn:oid:0.9.2342.19200300.100.1.3",
        "name": "urn:oid:2.5.4.42",
    })
    enabled: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS sso_configs (
    config_id     TEXT PRIMARY KEY,
    provider      TEXT NOT NULL,
    client_id     TEXT NOT NULL,
    client_secret TEXT NOT NULL,
    issuer_url    TEXT NOT NULL DEFAULT '',
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS saml_configs (
    config_id         TEXT PRIMARY KEY,
    idp_entity_id     TEXT NOT NULL,
    idp_sso_url       TEXT NOT NULL,
    idp_slo_url       TEXT NOT NULL DEFAULT '',
    idp_x509_cert     TEXT NOT NULL,
    sp_entity_id      TEXT NOT NULL,
    attribute_mapping JSONB NOT NULL DEFAULT '{"email": "urn:oid:0.9.2342.19200300.100.1.3", "name": "urn:oid:2.5.4.42"}',
    enabled           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Add SSO columns to tenants table if missing
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenants' AND column_name = 'sso_provider'
    ) THEN
        ALTER TABLE tenants ADD COLUMN sso_provider TEXT;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'tenants' AND column_name = 'sso_sub'
    ) THEN
        ALTER TABLE tenants ADD COLUMN sso_sub TEXT;
    END IF;
END $$;
"""

# In-memory state store for OAuth2 flows (PKCE + CSRF)
_PENDING_FLOWS: dict[str, dict[str, Any]] = {}


# ============================================================================
# SSOProvider
# ============================================================================

class SSOProvider:
    """Manages OIDC/OAuth2 SSO flows.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    portal_auth : PortalAuth
        The portal auth service for JWT issuance.
    redirect_base : str
        Base URL for OAuth callbacks (e.g. ``https://cloud.grafomem.com``).
    """

    def __init__(
        self,
        db_url: str,
        portal_auth=None,
        redirect_base: str = "",
        pool=None,
    ) -> None:
        self._db_url = db_url
        self._portal_auth = portal_auth
        self._redirect_base = redirect_base.rstrip("/")
        self._conn: psycopg.Connection[dict[str, Any]] | None = None
        self._pool = pool

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        if self._pool is not None:
            return self._pool.getconn()
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        if self._pool is not None:
            self._conn = None
            return
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    def ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.execute(_SCHEMA_SQL)
        logger.info("SSO schema ensured")

    # ------------------------------------------------------------------
    # SSO Configuration CRUD
    # ------------------------------------------------------------------

    def configure_provider(
        self,
        provider: str,
        client_id: str,
        client_secret: str,
        issuer_url: str = "",
    ) -> SSOConfig:
        """Configure an SSO provider (admin operation)."""
        config_id = uuid.uuid4().hex[:24]
        now = datetime.now(timezone.utc)

        conn = self._get_conn()
        # Upsert by provider
        conn.execute(
            "DELETE FROM sso_configs WHERE provider = %s",
            (provider,),
        )
        conn.execute(
            "INSERT INTO sso_configs "
            "(config_id, provider, client_id, client_secret, issuer_url, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (config_id, provider, client_id, client_secret, issuer_url, now),
        )
        logger.info("SSO provider configured: %s", provider)

        return SSOConfig(
            config_id=config_id,
            provider=provider,
            client_id=client_id,
            client_secret=client_secret,
            issuer_url=issuer_url,
            created_at=now,
        )

    def get_provider_config(self, provider: str) -> SSOConfig | None:
        """Get SSO config for a provider."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM sso_configs WHERE provider = %s AND enabled = TRUE",
            (provider,),
        ).fetchone()
        if row is None:
            return None
        return SSOConfig(
            config_id=row["config_id"],
            provider=row["provider"],
            client_id=row["client_id"],
            client_secret=row["client_secret"],
            issuer_url=row.get("issuer_url", ""),
            enabled=row.get("enabled", True),
            created_at=row["created_at"],
        )

    def list_providers(self) -> list[dict[str, Any]]:
        """List available SSO providers (without secrets)."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT config_id, provider, enabled, created_at FROM sso_configs "
            "ORDER BY provider",
        ).fetchall()

        configured = {r["provider"] for r in rows}
        result = []

        # Add well-known providers
        for name in sorted(PROVIDER_CONFIGS):
            result.append({
                "provider": name,
                "configured": name in configured,
                "well_known": True,
            })

        # Add custom providers
        for row in rows:
            if row["provider"] not in PROVIDER_CONFIGS:
                result.append({
                    "provider": row["provider"],
                    "configured": True,
                    "well_known": False,
                })

        return result

    # ------------------------------------------------------------------
    # SAML 2.0
    # ------------------------------------------------------------------

    def configure_saml(
        self,
        metadata_url: str | None = None,
        metadata_xml: str | None = None,
        sp_entity_id: str | None = None,
        attribute_mapping: dict | None = None,
    ) -> SAMLConfig:
        """Configure a SAML 2.0 IdP.

        Accepts either a metadata URL (auto-discovery) or raw metadata XML.
        Parses the XML to extract IdP entity ID, SSO URL, and X.509 certificate.

        Parameters
        ----------
        metadata_url : str, optional
            URL to fetch IdP metadata XML from.
        metadata_xml : str, optional
            Raw IdP metadata XML string.
        sp_entity_id : str, optional
            Override for the SP entity ID (defaults to redirect_base).
        attribute_mapping : dict, optional
            Mapping of GRAFOMEM fields to SAML attributes.
        """
        if not metadata_url and not metadata_xml:
            raise ValueError("Either metadata_url or metadata_xml is required")

        # Fetch metadata if URL provided
        if metadata_url and not metadata_xml:
            import httpx
            resp = httpx.get(metadata_url, timeout=15)
            if resp.status_code != 200:
                raise ValueError(f"Failed to fetch metadata from {metadata_url}: HTTP {resp.status_code}")
            metadata_xml = resp.text

        # Parse metadata XML
        idp_entity_id, idp_sso_url, idp_slo_url, idp_cert = self._parse_saml_metadata(metadata_xml)

        config_id = uuid.uuid4().hex[:24]
        entity_id = sp_entity_id or f"{self._redirect_base}/v1/portal/sso/saml/metadata"
        mapping = attribute_mapping or {
            "email": "urn:oid:0.9.2342.19200300.100.1.3",
            "name": "urn:oid:2.5.4.42",
        }

        conn = self._get_conn()
        # Replace any existing config
        conn.execute("DELETE FROM saml_configs WHERE TRUE")
        conn.execute(
            "INSERT INTO saml_configs "
            "(config_id, idp_entity_id, idp_sso_url, idp_slo_url, idp_x509_cert, "
            " sp_entity_id, attribute_mapping, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (config_id, idp_entity_id, idp_sso_url, idp_slo_url, idp_cert,
             entity_id, json.dumps(mapping), datetime.now(timezone.utc)),
        )
        logger.info("SAML IdP configured: %s", idp_entity_id)

        return SAMLConfig(
            config_id=config_id,
            idp_entity_id=idp_entity_id,
            idp_sso_url=idp_sso_url,
            idp_slo_url=idp_slo_url,
            idp_x509_cert=idp_cert,
            sp_entity_id=entity_id,
            attribute_mapping=mapping,
        )

    def get_saml_config(self) -> SAMLConfig | None:
        """Get the current SAML IdP configuration."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM saml_configs WHERE enabled = TRUE LIMIT 1",
        ).fetchone()
        if row is None:
            return None
        return SAMLConfig(
            config_id=row["config_id"],
            idp_entity_id=row["idp_entity_id"],
            idp_sso_url=row["idp_sso_url"],
            idp_slo_url=row.get("idp_slo_url", ""),
            idp_x509_cert=row["idp_x509_cert"],
            sp_entity_id=row["sp_entity_id"],
            attribute_mapping=row.get("attribute_mapping") or {},
            enabled=row.get("enabled", True),
            created_at=row["created_at"],
        )

    def get_sp_metadata(self) -> str:
        """Generate SAML 2.0 SP metadata XML.

        Returns an EntityDescriptor with the AssertionConsumerService URL.
        """
        acs_url = f"{self._redirect_base}/v1/portal/sso/saml/acs"
        entity_id = f"{self._redirect_base}/v1/portal/sso/saml/metadata"

        # Check if there's a configured SP entity ID
        config = self.get_saml_config()
        if config:
            entity_id = config.sp_entity_id

        return f"""<?xml version="1.0" encoding="UTF-8"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     entityID="{entity_id}">
  <md:SPSSODescriptor
      AuthnRequestsSigned="false"
      WantAssertionsSigned="true"
      protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>
    <md:AssertionConsumerService
        Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
        Location="{acs_url}"
        index="1"
        isDefault="true"/>
  </md:SPSSODescriptor>
</md:EntityDescriptor>"""

    def initiate_saml_flow(self) -> str:
        """Build a SAML AuthnRequest and return the IdP redirect URL."""
        config = self.get_saml_config()
        if config is None:
            raise ValueError("SAML is not configured")

        import base64
        import zlib
        from urllib.parse import urlencode

        request_id = f"_gfm_{uuid.uuid4().hex}"
        issue_instant = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        acs_url = f"{self._redirect_base}/v1/portal/sso/saml/acs"

        authn_request = f"""<samlp:AuthnRequest
    xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
    xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
    ID="{request_id}"
    Version="2.0"
    IssueInstant="{issue_instant}"
    Destination="{config.idp_sso_url}"
    AssertionConsumerServiceURL="{acs_url}"
    ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST">
  <saml:Issuer>{config.sp_entity_id}</saml:Issuer>
  <samlp:NameIDPolicy Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress" AllowCreate="true"/>
</samlp:AuthnRequest>"""

        # Store flow state for response validation
        _PENDING_FLOWS[request_id] = {
            "provider": "saml",
            "created_at": issue_instant,
        }

        # DEFLATE + base64 encode for HTTP-Redirect binding
        compressed = zlib.compress(authn_request.encode())[2:-4]  # raw deflate
        encoded = base64.b64encode(compressed).decode()

        params = urlencode({
            "SAMLRequest": encoded,
            "RelayState": self._redirect_base + "/portal",
        })

        url = f"{config.idp_sso_url}?{params}"
        logger.info("SAML AuthnRequest issued (ID=%s)", request_id)
        return url

    def handle_saml_response(self, saml_response: str, relay_state: str = "") -> dict:
        """Process a SAML Response from the IdP.

        Validates the response, extracts the NameID and attributes,
        and resolves the user to a GRAFOMEM tenant.

        Parameters
        ----------
        saml_response : str
            Base64-encoded SAML Response (from the POST form data).
        relay_state : str
            The RelayState parameter from the IdP.

        Returns
        -------
        dict
            Same shape as handle_callback: {token, tenant_id, email, ...}
        """
        import base64
        from xml.etree import ElementTree as ET

        config = self.get_saml_config()
        if config is None:
            raise ValueError("SAML is not configured")

        # Decode the SAML Response
        try:
            response_xml = base64.b64decode(saml_response).decode()
        except Exception as e:
            raise ValueError(f"Invalid SAML Response encoding: {e}")

        # Parse and extract NameID and attributes
        ns = {
            "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
            "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
        }

        try:
            root = ET.fromstring(response_xml)
        except ET.ParseError as e:
            raise ValueError(f"Malformed SAML Response XML: {e}")

        # Check status
        status_code = root.find(".//samlp:StatusCode", ns)
        if status_code is not None:
            status_value = status_code.get("Value", "")
            if "Success" not in status_value:
                raise ValueError(f"SAML authentication failed: {status_value}")

        # Extract NameID
        name_id_el = root.find(".//saml:NameID", ns)
        if name_id_el is None or not name_id_el.text:
            raise ValueError("No NameID in SAML Response")
        name_id = name_id_el.text.strip()

        # Extract attributes
        email = name_id  # NameID is usually the email
        name = name_id.split("@")[0]

        for attr_stmt in root.findall(".//saml:AttributeStatement/saml:Attribute", ns):
            attr_name = attr_stmt.get("Name", "")
            attr_value_el = attr_stmt.find("saml:AttributeValue", ns)
            if attr_value_el is None or not attr_value_el.text:
                continue
            val = attr_value_el.text.strip()

            # Match against configured attribute mapping
            mapping = config.attribute_mapping or {}
            if attr_name == mapping.get("email") or "mail" in attr_name.lower():
                email = val
            elif attr_name == mapping.get("name") or "givenname" in attr_name.lower():
                name = val

        if not email or "@" not in email:
            raise ValueError(f"Could not extract valid email from SAML Response (got: {email!r})")

        # Extract IdP entity ID for sub
        issuer_el = root.find("saml:Issuer", ns)
        sso_sub = name_id
        if issuer_el is not None and issuer_el.text:
            sso_sub = f"{issuer_el.text.strip()}|{name_id}"

        # Find or create tenant
        tenant_id, api_key = self._find_or_create_tenant(
            email=email,
            name=name,
            sso_provider="saml",
            sso_sub=sso_sub,
        )

        # Issue JWT
        token = None
        if self._portal_auth:
            token = self._portal_auth._issue_jwt(tenant_id, email)

        logger.info("SAML login: %s → tenant %s", email, tenant_id)
        try:
            from aml.cloud.metrics import SSO_LOGINS
            SSO_LOGINS.labels(provider="saml").inc()
        except Exception:
            pass

        return {
            "token": token,
            "tenant_id": tenant_id,
            "email": email,
            "name": name,
            "sso_provider": "saml",
            "api_key": api_key,
        }

    @staticmethod
    def _parse_saml_metadata(xml_str: str) -> tuple[str, str, str, str]:
        """Parse IdP metadata XML.

        Returns (entity_id, sso_url, slo_url, x509_cert).
        """
        from xml.etree import ElementTree as ET

        ns = {
            "md": "urn:oasis:names:tc:SAML:2.0:metadata",
            "ds": "http://www.w3.org/2000/09/xmldsig#",
        }

        root = ET.fromstring(xml_str)
        entity_id = root.get("entityID", "")

        sso_url = ""
        slo_url = ""
        cert = ""

        # Find SSO service (HTTP-Redirect or HTTP-POST)
        for sso in root.findall(".//md:IDPSSODescriptor/md:SingleSignOnService", ns):
            binding = sso.get("Binding", "")
            if "HTTP-Redirect" in binding or "HTTP-POST" in binding:
                sso_url = sso.get("Location", "")
                break

        # Find SLO service
        for slo in root.findall(".//md:IDPSSODescriptor/md:SingleLogoutService", ns):
            slo_url = slo.get("Location", "")
            break

        # Extract signing certificate
        cert_el = root.find(".//md:IDPSSODescriptor/md:KeyDescriptor/ds:KeyInfo/ds:X509Data/ds:X509Certificate", ns)
        if cert_el is not None and cert_el.text:
            cert = cert_el.text.strip()

        if not entity_id or not sso_url:
            raise ValueError("Invalid IdP metadata: missing entityID or SingleSignOnService")

        return entity_id, sso_url, slo_url, cert

    # ------------------------------------------------------------------
    # OAuth2 Flow
    # ------------------------------------------------------------------

    def initiate_flow(self, provider: str) -> str:
        """Start an OAuth2/OIDC authorization flow.

        Returns the authorization URL to redirect the user to.
        """
        config = self.get_provider_config(provider)
        if config is None:
            raise ValueError(f"SSO provider '{provider}' is not configured")

        provider_info = PROVIDER_CONFIGS.get(provider, {})
        authorize_url = provider_info.get("authorize_url", config.issuer_url + "/authorize")
        scopes = provider_info.get("scopes", ["openid", "email", "profile"])

        state = secrets.token_urlsafe(32)
        redirect_uri = f"{self._redirect_base}/v1/portal/sso/callback"

        # Store flow state
        _PENDING_FLOWS[state] = {
            "provider": provider,
            "redirect_uri": redirect_uri,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        params = {
            "client_id": config.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
        }

        if provider != "github":
            params["access_type"] = "offline"

        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{authorize_url}?{query}"

        logger.info("SSO flow initiated for %s (state=%s...)", provider, state[:8])
        return url

    def handle_callback(
        self,
        code: str,
        state: str,
    ) -> dict[str, Any]:
        """Handle the OAuth2 callback after user authorization.

        Exchanges the code for tokens, fetches user info, and
        finds/creates a GRAFOMEM tenant.

        Returns
        -------
        dict
            ``{"token": "jwt...", "tenant_id": "...", "email": "...",
               "name": "...", "sso_provider": "..."}``
        """
        # Validate state
        flow = _PENDING_FLOWS.pop(state, None)
        if flow is None:
            raise ValueError("Invalid or expired OAuth state parameter")

        provider = flow["provider"]
        redirect_uri = flow["redirect_uri"]

        config = self.get_provider_config(provider)
        if config is None:
            raise ValueError(f"SSO provider '{provider}' is not configured")

        provider_info = PROVIDER_CONFIGS.get(provider, {})
        token_url = provider_info.get("token_url", config.issuer_url + "/token")
        userinfo_url = provider_info.get("userinfo_url", config.issuer_url + "/userinfo")

        # Exchange code for tokens
        import httpx

        token_data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
        }

        headers = {"Accept": "application/json"}
        resp = httpx.post(token_url, data=token_data, headers=headers, timeout=10)
        if resp.status_code != 200:
            raise ValueError(f"Token exchange failed: {resp.text}")

        tokens = resp.json()
        access_token = tokens.get("access_token")
        if not access_token:
            raise ValueError("No access_token in response")

        # Fetch user info
        user_resp = httpx.get(
            userinfo_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if user_resp.status_code != 200:
            raise ValueError(f"User info fetch failed: {user_resp.text}")

        user_info = user_resp.json()

        # Extract email and name
        email = user_info.get("email")
        if not email and provider == "github":
            # GitHub: fetch email from /user/emails
            emails_resp = httpx.get(
                "https://api.github.com/user/emails",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if emails_resp.status_code == 200:
                for e in emails_resp.json():
                    if e.get("primary"):
                        email = e.get("email")
                        break

        if not email:
            raise ValueError("Could not determine user email from SSO provider")

        name = (
            user_info.get("name")
            or user_info.get("login")
            or email.split("@")[0]
        )
        sso_sub = str(user_info.get("sub") or user_info.get("id") or email)

        # Find or create tenant
        tenant_id, api_key = self._find_or_create_tenant(
            email=email,
            name=name,
            sso_provider=provider,
            sso_sub=sso_sub,
        )

        # Issue GRAFOMEM JWT
        token = None
        if self._portal_auth:
            token = self._portal_auth._issue_jwt(tenant_id, email)

        logger.info(
            "SSO login: %s via %s → tenant %s",
            email, provider, tenant_id,
        )
        try:
            from aml.cloud.metrics import SSO_LOGINS
            SSO_LOGINS.labels(provider=provider).inc()
        except Exception:
            pass

        return {
            "token": token,
            "tenant_id": tenant_id,
            "email": email,
            "name": name,
            "sso_provider": provider,
            "api_key": api_key,
        }

    # ------------------------------------------------------------------
    # Tenant resolution
    # ------------------------------------------------------------------

    def _find_or_create_tenant(
        self,
        email: str,
        name: str,
        sso_provider: str,
        sso_sub: str,
    ) -> tuple[str, str]:
        """Find an existing tenant by email or SSO sub, or create a new one.

        Returns (tenant_id, api_key).
        """
        conn = self._get_conn()

        # Try to find by SSO sub
        row = conn.execute(
            "SELECT id, api_key FROM tenants "
            "WHERE sso_provider = %s AND sso_sub = %s",
            (sso_provider, sso_sub),
        ).fetchone()
        if row:
            return row["id"], row["api_key"]

        # Try to find by email
        row = conn.execute(
            "SELECT id, api_key FROM tenants WHERE email = %s",
            (email,),
        ).fetchone()
        if row:
            # Link SSO to existing tenant
            conn.execute(
                "UPDATE tenants SET sso_provider = %s, sso_sub = %s WHERE id = %s",
                (sso_provider, sso_sub, row["id"]),
            )
            return row["id"], row["api_key"]

        # Create new tenant
        import secrets as sec
        tenant_id = uuid.uuid4().hex
        api_key = f"gfm_{sec.token_hex(24)}"
        now = datetime.now(timezone.utc)

        conn.execute(
            "INSERT INTO tenants "
            "(id, name, api_key, plan, created_at, email, sso_provider, sso_sub) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (tenant_id, name, api_key, "starter", now, email,
             sso_provider, sso_sub),
        )
        logger.info(
            "New tenant created via SSO: %s (%s via %s)",
            tenant_id, email, sso_provider,
        )
        return tenant_id, api_key
