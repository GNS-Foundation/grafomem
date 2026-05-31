#!/usr/bin/env python3
"""
tests/test_v3_conformance.py — CI entry point for the GRAFOMEM v3.0 governed-services suites.

Runs all six two-sided self-conformance suites (R1-R5 + gcrumbs) against the database in
GRAFOMEM_DB_URL. Discoverable by pytest as six parametrized cases; also runnable directly.
Skips cleanly (rather than failing) when GRAFOMEM_DB_URL is unset, so unit-only CI stages pass.

Note: B0 (test_gcrumbs_b0.py) runs DB-free and is NOT included here — it always runs.

    GRAFOMEM_DB_URL=postgresql://grafomem:grafomem_dev@localhost:5432/grafomem pytest tests/test_v3_conformance.py -v
    GRAFOMEM_DB_URL=...                                                        python3 tests/test_v3_conformance.py
"""
import os, sys, pathlib, importlib.util

SUITES = [
    "landing_self_conformance.py",                 # R3
    "artifact_registry_self_conformance.py",       # R1
    "world_model_self_conformance.py",             # R5
    "provenance_customs_self_conformance.py",      # R2
    "composition_governance_self_conformance.py",  # R4
    "gcrumbs_self_conformance.py",                 # gcrumbs (B1-B10)
]
_HERE = pathlib.Path(__file__).resolve().parent


def _load_main(suite: str):
    """Load a suite by file path (robust to package layout) and return its main()."""
    path = _HERE / suite
    spec = importlib.util.spec_from_file_location(suite[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)        # defines functions only; main() is __main__-guarded
    return mod.main


try:
    import pytest

    pytestmark = pytest.mark.skipif(
        not os.environ.get("GRAFOMEM_DB_URL"),
        reason="set GRAFOMEM_DB_URL to run the v3 governed-services conformance suites",
    )

    @pytest.mark.parametrize("suite", SUITES)
    def test_v3_self_conformance(suite):
        assert _load_main(suite)() == 0, f"{suite}: one or more conformance gates failed"

except ImportError:
    pass  # pytest absent — the direct-run block below still works


if __name__ == "__main__":
    if not os.environ.get("GRAFOMEM_DB_URL"):
        print("set GRAFOMEM_DB_URL to run the suites"); sys.exit(2)
    failed = [s for s in SUITES if _load_main(s)() != 0]
    print(f"\n=== v3 conformance: {len(SUITES) - len(failed)}/{len(SUITES)} suites green "
          f"({(len(SUITES) - len(failed)) * 10}/{len(SUITES) * 10} gates) ===")
    if failed:
        print("FAILED:", ", ".join(failed))
    sys.exit(1 if failed else 0)
