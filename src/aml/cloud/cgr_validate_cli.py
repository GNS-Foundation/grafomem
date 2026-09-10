"""Live-substrate CGR validation — the **product side** of the standard/product boundary.

`aml.cgr` is the standard and must not import `aml.cloud` (product code), so it
cannot construct `DecisionTrailService` for itself. It exposes the pure,
provider-injected `validate_live(decision_trail, store_manager, tenant_id)`;
this module supplies the concrete cloud providers and calls it. The dependency
therefore runs product -> standard, the only direction allowed.

Imports stay inside `main()` (as they did when this wiring lived in
`aml.cgr.validate._run_live`) so importing this module never drags the server
and backend stack in behind it.

CLI:  python -m aml.cloud.cgr_validate_cli --tenant <tenant_id>   # needs GRAFOMEM_DB_URL
"""
from __future__ import annotations

import argparse
import os


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="CGR-v1 validation report over a live tenant's substrate")
    ap.add_argument("--tenant", required=True,
                    help="tenant_id to validate (needs GRAFOMEM_DB_URL)")
    args = ap.parse_args(argv)

    from aml.backends.postgres_gmp import PostgresGMPBackend
    from aml.cgr.validate import format_report, validate_live
    from aml.cloud.decision_trail import DecisionTrailService
    from aml.server.stores import StoreManager

    db = os.environ["GRAFOMEM_DB_URL"]
    decision_trail = DecisionTrailService(db)
    store_manager = StoreManager(lambda: PostgresGMPBackend(db))

    rep = validate_live(decision_trail, store_manager, args.tenant)
    print(format_report(rep, f"— tenant {args.tenant[:12]} (live)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
