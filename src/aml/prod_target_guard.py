"""Refuse to run a tenant-creating suite against a production host.

Shared by the conformance/resilience harnesses (`tests/sandbox_e2e_v2.py`,
`scripts/run_live_resilience.py`, `scripts/verify_finding1.py`). These suites
call `/v1/portal/signup` and create **real** tenants; pointed at production they
seed prod with test tenants (as happened 31 May–28 Jun; see the erasure-ledger
incident and the I0c backfill).

The override is a **per-invocation `--allow-prod` flag** — preferred, because an
exported env var silently blesses every later run in the same shell. An exported
`GRAFOMEM_ALLOW_PROD=1` is also honoured for non-interactive/CI use.
"""
from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

#: Hosts that must never be targeted implicitly.
PROD_HOSTS = ("api.grafomem.com", "grafomem-production.up.railway.app")


def is_prod_host(url: str) -> bool:
    """True if *url*'s host is a known production host."""
    return (urlparse(url).hostname or "") in PROD_HOSTS


def prod_allowed(argv: list[str] | None = None) -> bool:
    """True if prod was explicitly allowed: `--allow-prod` in argv, or GRAFOMEM_ALLOW_PROD=1."""
    argv = sys.argv if argv is None else argv
    return "--allow-prod" in argv or os.environ.get("GRAFOMEM_ALLOW_PROD") == "1"


def guard_not_prod(url: str, argv: list[str] | None = None) -> None:
    """Raise SystemExit unless *url* is non-prod or prod was explicitly allowed.

    The per-invocation `--allow-prod` flag is the intended override; the env var
    is a secondary escape hatch for CI.
    """
    if is_prod_host(url) and not prod_allowed(argv):
        host = urlparse(url).hostname or url
        raise SystemExit(
            f"refusing to run against production host {host!r} — this suite creates real tenants. "
            "Pass --allow-prod (per invocation) or set GRAFOMEM_ALLOW_PROD=1 to override."
        )
