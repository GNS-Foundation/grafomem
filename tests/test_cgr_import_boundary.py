"""`aml.cgr` (the standard) must not import `aml.cloud` (product code) — at any depth.

The dependency between the two is allowed to run **product -> standard** and
nothing else. A standard package that imports the product cannot be published,
audited, or verified on its own, which is precisely what the Python verifier on
PyPI needs it to be.

The check is **static (ast)** on purpose. The import this test replaced —
`aml.cgr.validate._run_live` -> `aml.cloud.decision_trail` — was *function-local*,
so it never appeared in `sys.modules` unless the live path ran: a runtime
`sys.modules` assertion would have passed while the boundary was broken. Walking
the AST catches a lazy import exactly as well as a top-of-file one.

The detector carries its own non-vacuity probe (`test_detector_actually_detects`):
a scanner that silently stopped matching would otherwise turn this file into a
test that always passes.
"""
from __future__ import annotations

import ast
import pathlib

FORBIDDEN = "aml.cloud"
CGR_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "aml" / "cgr"


def _forbidden_imports(source: str, label: str) -> list[str]:
    """Every import of `aml.cloud` (or a submodule) in `source`, as 'label:lineno -> target'."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == FORBIDDEN or alias.name.startswith(FORBIDDEN + "."):
                    hits.append(f"{label}:{node.lineno} -> import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0:
                # absolute:  from aml.cloud[.x] import y
                bad = module == FORBIDDEN or module.startswith(FORBIDDEN + ".")
            else:
                # relative from inside aml/cgr/:  level 2 == the `aml` package,
                # so `from ..cloud[.x] import y` resolves to aml.cloud[.x].
                bad = node.level >= 2 and (module == "cloud" or module.startswith("cloud."))
            if bad:
                dots = "." * node.level
                hits.append(f"{label}:{node.lineno} -> from {dots}{module} import ...")
    return hits


def test_cgr_does_not_import_aml_cloud():
    files = sorted(CGR_DIR.rglob("*.py"))
    assert files, f"no python files found under {CGR_DIR} — the scan would pass vacuously"

    violations: list[str] = []
    for path in files:
        rel = path.relative_to(CGR_DIR.parents[2])   # -> src/aml/cgr/<file>.py
        violations += _forbidden_imports(path.read_text(encoding="utf-8"), str(rel))

    assert not violations, (
        "the standard imports the product — `aml.cgr` must not import `aml.cloud`:\n  "
        + "\n  ".join(violations)
        + "\n\nInject the provider instead (see aml.cgr.validate.validate_live) and put the"
          "\nconcrete wiring on the product side (see aml.cloud.cgr_validate_cli)."
    )


def test_detector_actually_detects():
    """Non-vacuity: the scanner must flag every shape of the import it forbids."""
    cases = [
        "import aml.cloud",
        "import aml.cloud.decision_trail",
        "from aml.cloud import decision_trail",
        "from aml.cloud.decision_trail import DecisionTrailService",
        "def f():\n    from aml.cloud.decision_trail import DecisionTrailService\n",  # lazy
        "from ..cloud.decision_trail import DecisionTrailService",                    # relative
    ]
    for src in cases:
        assert _forbidden_imports(src, "<probe>"), f"scanner missed: {src!r}"

    # ...and must not flag imports that are fine.
    for src in ["import aml.cgr.engine",
                "from aml.cgr.substrate import DecisionRow",
                "from aml.server.stores import StoreManager",
                "from .substrate import DecisionRow",
                "import cloudpickle"]:
        assert not _forbidden_imports(src, "<probe>"), f"scanner false-positived: {src!r}"
