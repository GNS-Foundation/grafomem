"""Scoped audit — the only audit a request handler may use (0.4.1).

`MemoryBackend.audit()` lists EVERY memory of a backend. Stores of one server can share one physical table (the SQLite
backend on one database file, the PostgreSQL backend's `memories` table), so an unscoped audit reachable from an HTTP
or MCP handler exposes other tenants' memories and the tenant's other stores. Handlers call `scoped_audit()` below;
the unscoped `audit()` stays for backend self-tests, the conformance suite and the GMP reference wire binding, and
tests/test_audit_scope.py fails if a handler calls it.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("grafomem.audit_scope")


def belongs_to(memory_tenant: str | None, tenant_id: str | None, include_subnamespaces: bool) -> bool:
    """A memory belongs to `tenant_id` when its tenant is exactly it or, with include_subnamespaces, `tenant_id/<sub>`.
    `None` is the untenanted (single-tenant / default) namespace and matches only untenanted memories."""
    mt = memory_tenant or None
    if tenant_id is None:
        return mt is None
    return mt == tenant_id or (include_subnamespaces and mt is not None and mt.startswith(tenant_id + "/"))


def scoped_audit(backend: Any, tenant_id: str | None, store_id: str | None = None, *, include_subnamespaces: bool = False) -> list:
    """The memories of `tenant_id` (and, when given, of store `store_id`) in `backend`, superseded included.

    Backends that keep several stores in one table implement `scoped_audit(tenant_id, store_id=..., ...)` and filter in
    their query (SQLite, PostgreSQL). Any other backend is one in-memory instance per store, so the instance IS the
    store; the tenant filter is applied here."""
    fn = getattr(backend, "scoped_audit", None)
    if callable(fn):
        if store_id is None and not include_subnamespaces:
            return list(fn(tenant_id))   # the CGR #12 signature, scoped_audit(tenant_id), stays valid
        return list(fn(tenant_id, store_id=store_id, include_subnamespaces=include_subnamespaces))
    return [m for m in backend.audit() if belongs_to(m.tenant_id, tenant_id, include_subnamespaces)]


def bind_store(backend: Any, store_id: str) -> None:
    """Tell a backend which store it serves, so that it records the store on every new memory (shared-table backends)."""
    fn = getattr(backend, "bind_store", None)
    if callable(fn):
        fn(store_id)
