#!/usr/bin/env python3
"""GRAFOMEM-internal entry point for the grafomem-cgr capture MCP server.

The implementation now lives in the published package `grafomem-cgr`
(packages/grafomem-cgr/) so a stranger can run it with `uvx grafomem-cgr` and no
repo checkout. This module is the INTERNAL wrapper: it pins GRAFOMEM's own policy
— the corp tenant is on the forbidden denylist — and is what the dogfood launcher
and the ops docs point at.

Behaviour for the dogfood loop is unchanged: corp is refused at config load and at
runtime, exactly as before. The difference is only that "corp" is now GRAFOMEM's
own configuration of a general denylist, rather than a constant baked into a
package other people install.
"""
from __future__ import annotations

import os
import sys

# Make the in-repo package importable without installing it (dev/dogfood convenience).
_PKG_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "packages", "grafomem-cgr", "src")
if _PKG_SRC not in sys.path:
    sys.path.insert(0, _PKG_SRC)

from grafomem_cgr.capture import (  # noqa: E402  (path shim must precede the import)
    DEV_DOMAINS,
    DEV_OUTCOME_MAP,
    CaptureClient,
    Config,
    _assert_not_forbidden,
    build_mcp_server,
    map_dev_outcome,
)
from grafomem_cgr.capture import main as _pkg_main  # noqa: E402

# GRAFOMEM's corp tenant — off-limits for the dogfood capture loop. This is OUR
# policy, so it lives here in ops/, not in the public package. Mirrors govern_dev.py.
CORP_TENANT = "5605470cfa8e415ba418c9d8944abf9a"

__all__ = ["CORP_TENANT", "DEV_DOMAINS", "DEV_OUTCOME_MAP", "CaptureClient", "Config",
           "_assert_not_corp", "_assert_not_forbidden", "build_mcp_server",
           "map_dev_outcome", "main"]


def _assert_not_corp(tenant_id: str, *, where: str) -> None:
    """GRAFOMEM's never-corp guard, expressed via the general denylist."""
    _assert_not_forbidden(tenant_id, {CORP_TENANT}, where=where)


def main(argv=None) -> None:
    # Always deny corp for anything launched through ops/, whatever else is configured.
    existing = os.environ.get("GRAFOMEM_CGR_FORBIDDEN_TENANTS", "")
    forbidden = {t.strip() for t in existing.split(",") if t.strip()} | {CORP_TENANT}
    os.environ["GRAFOMEM_CGR_FORBIDDEN_TENANTS"] = ",".join(sorted(forbidden))
    _pkg_main(argv)


if __name__ == "__main__":
    main()
