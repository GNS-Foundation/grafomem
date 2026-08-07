"""Central tenant context for Postgres RLS enforcement (Track 1).

A single mechanism so NO query can forget its tenant scope:

  * `current_tenant` — a ContextVar set once per request by the auth middleware
    (from the authenticated `request.state.tenant`) and set explicitly by background
    jobs to a scoped value. Async tasks inherit it within the request.

  * `apply_tenant_context(conn)` — executed on EVERY Postgres connection checkout
    (wired into each pool / getconn boundary). It sets the Postgres GUC
    `app.current_tenant` that the RLS policies read. Works for psycopg3 AND psycopg2
    (both expose `conn.cursor()` / `cur.execute`).

FAIL-CLOSED: when no tenant is in context the GUC is set to '' (empty), which matches
no `tenant_id` — so a code path that somehow skips the middleware sees ZERO rows, never
another tenant's rows. A propagation miss degrades to no-data, never cross-tenant data.

The GUC is set SESSION-level (is_local=false) and overwritten on every checkout, so a
pooled connection reused across requests can't carry a prior tenant's scope; `reset_tenant_context`
additionally clears it on return to the pool as defence in depth.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

# None ⇒ "no tenant established" ⇒ fail-closed ('' GUC ⇒ 0 rows).
current_tenant: ContextVar[Optional[str]] = ContextVar("current_tenant", default=None)

# NOTE: there is deliberately NO system/'admin' sentinel. Every policied-table access is
# tenant-scoped: in-request paths inherit the request tenant; background paths (push_service,
# erasure) set their own tenant; the Merkle/anchor job touches NO policied table (verified),
# so no cross-tenant door exists. IDOR is closed by construction — do not add a blanket value.

_GUC = "app.current_tenant"


def set_current_tenant(tenant_id: Optional[str]) -> object:
    """Set the request/task tenant. Returns the Token for optional reset."""
    return current_tenant.set(tenant_id)


def get_current_tenant() -> Optional[str]:
    return current_tenant.get()


def _guc_value() -> str:
    t = current_tenant.get()
    # fail-closed: no context ⇒ '' ⇒ matches no tenant_id
    return t if t else ""


def apply_tenant_context(conn) -> None:
    """SET the app.current_tenant GUC on `conn` from the ContextVar. Call on checkout.

    Driver-agnostic (psycopg3 + psycopg2). Session-level so it survives the operation's
    transaction boundaries; overwritten on the next checkout. Never raises into the caller
    on a benign context issue — but a failure to set MUST fail closed, so we set '' first
    and only then the real value."""
    with conn.cursor() as cur:
        cur.execute("SELECT set_config(%s, %s, false)", (_GUC, _guc_value()))


def reset_tenant_context(conn) -> None:
    """Clear the GUC to fail-closed '' when returning a connection to the pool."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config(%s, '', false)", (_GUC,))
    except Exception:
        pass
