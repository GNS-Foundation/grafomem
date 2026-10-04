"""B4: the SSO existing-tenant paths return (tenant_id, None) explicitly — by SSO sub and by
email-link — now that the portal_auth no-op helper (always None since hash-at-rest PR 5) is gone.
No behaviour change: the SSO response's api_key was already None on these paths."""
import os
import uuid

import pytest

from aml.cloud.sso_provider import SSOProvider
from aml.cloud.tenant_manager import TenantManager

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")


@pytest.fixture(scope="module")
def provider():
    TenantManager(DB_URL).ensure_schema()
    p = SSOProvider(DB_URL)
    p.ensure_schema()
    return p


def test_existing_tenant_by_sub_returns_none_key(provider):
    email = f"sso-sub-{uuid.uuid4().hex[:8]}@example.test"
    sub = f"sub-{uuid.uuid4().hex[:8]}"
    tid, first_key = provider._find_or_create_tenant(email, "SSO User", "google", sub)
    assert first_key and first_key.startswith("gfm_"), "create is show-once: a key is minted"
    tid2, key2 = provider._find_or_create_tenant(email, "SSO User", "google", sub)
    assert tid2 == tid
    assert key2 is None


def test_existing_tenant_by_email_link_returns_none_key(provider):
    """A tenant that exists with this email but no SSO sub yet: linked, same id, api_key None."""
    import psycopg
    email = f"sso-link-{uuid.uuid4().hex[:8]}@example.test"
    t = TenantManager(DB_URL).create_tenant(name="Legacy Org")
    with psycopg.connect(DB_URL, autocommit=True) as c:
        c.execute("UPDATE tenants SET email = %s WHERE id = %s", (email, t.id))
    sub = f"sub-{uuid.uuid4().hex[:8]}"
    tid, key = provider._find_or_create_tenant(email, "Legacy Org", "google", sub)
    assert tid == t.id
    assert key is None
    with psycopg.connect(DB_URL) as c:
        row = c.execute("SELECT sso_provider, sso_sub FROM tenants WHERE id = %s", (t.id,)).fetchone()
    assert row == ("google", sub), "the link was recorded"
