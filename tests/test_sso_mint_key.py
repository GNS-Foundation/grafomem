"""014 step (a): the SSO path mints a tenant_api_keys row and never uses tenants.api_key.

Before #160 an SSO tenant got only tenants.api_key and no key row; after #160 removed the
tenants.api_key auth fallback, such a tenant could not authenticate at all. 014 step (a)
fixes this: SSO create MINTS a tenant_api_keys row, and the existing-tenant branches read
the working key from tenant_api_keys (not the now-NULL tenants.api_key).
"""
import os
import uuid

import psycopg
import pytest

from aml.cloud.tenant_manager import TenantManager
from aml.cloud.sso_provider import SSOProvider

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")


@pytest.fixture(scope="module")
def provider():
    TenantManager(DB_URL).ensure_schema()      # tenants + tenant_api_keys
    p = SSOProvider(DB_URL)
    p.ensure_schema()                           # sso_provider / sso_sub columns
    return p


def _row(sql, args):
    with psycopg.connect(DB_URL) as c:
        r = c.execute(sql, args).fetchone()
        return r


def test_sso_create_mints_key_row_and_never_uses_tenants_api_key(provider):
    email = f"sso-{uuid.uuid4().hex[:8]}@example.test"
    sub = f"sub-{uuid.uuid4().hex[:8]}"

    # First login → creates the tenant.
    tid, key = provider._find_or_create_tenant(email, "SSO User", "google", sub)
    assert key and key.startswith("gfm_"), f"SSO create returned no usable key: {key!r}"

    # A tenant_api_keys row was minted with that exact key.
    kr = _row("SELECT api_key, role FROM tenant_api_keys WHERE tenant_id = %s", (tid,))
    assert kr is not None, "SSO create did not mint a tenant_api_keys row"
    assert kr[0] == key

    # tenants.api_key was NOT written (014 step a).
    tr = _row("SELECT api_key FROM tenants WHERE id = %s", (tid,))
    assert tr is not None and tr[0] is None, f"tenants.api_key should be NULL, got {tr and tr[0]!r}"

    # Second login (same sub) → existing-tenant branch must return the SAME key,
    # read from tenant_api_keys (not the NULL tenants.api_key).
    tid2, key2 = provider._find_or_create_tenant(email, "SSO User", "google", sub)
    assert tid2 == tid
    assert key2 == key, "repeat SSO login did not return the working key from tenant_api_keys"
