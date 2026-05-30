"""Phase-2 — the code half of the GNS world-model: types + lightweight extractors.
Dependency-free static extraction (file walk + regex), so it runs anywhere.
"""
import re
from pathlib import Path

CODE_OBJECT_TYPES = [
    ("Module", ["name", "n_files"], ["name"]),
    ("File", ["path", "content_hash", "loc"], ["path"]),
    ("Endpoint", ["method", "route"], ["method", "route"]),
    ("Table", ["name"], ["name"]),
    ("Test", ["path"], ["path"]),
    ("License", ["spdx", "content_hash"], ["spdx"]),
]
CODE_LINK_TYPES = [
    ("belongs_to", "File", "Module", "N:1"),
    ("defined_in", "Endpoint", "File", "1:1"),
    ("licensed_under", "File", "License", "N:1"),
    ("test_in", "Test", "File", "1:1"),
]
CODE_ACTION_TYPES = [
    ("ingest_repo", "create", ["File", "Module"], "operate", True, False),
    ("attest_license", "create", ["License"], "operate", True, False),
    ("compose_sources", "create", ["Module"], "curate", True, False),
]

_ROUTE = re.compile(r"@\w+\.(get|post|put|delete|patch)\(\s*[\"']([^\"']+)", re.I)
_TABLE = re.compile(r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+[\"']?(\w+)", re.I)


def declare_code_interface(wm):
    for name, props, idp in CODE_OBJECT_TYPES:
        if name not in wm.object_types:
            wm.declare_object_type(name, props, idp)
    for name, frm, to, card in CODE_LINK_TYPES:
        if name not in wm.link_types:
            wm.declare_link_type(name, frm, to, card)
    for name, effect, targets, tier, deleg, hitl in CODE_ACTION_TYPES:
        if name not in wm.action_types:
            wm.declare_action_type(name, effect, targets, tier, requires_deleg=deleg, hitl=hitl)


def iter_code_files(root: Path):
    skip = {"__pycache__", ".venv", "venv", ".git", "node_modules", "dist", "build"}
    for p in sorted(root.rglob("*.py")):
        if not any(part in skip for part in p.parts):
            yield p


def extract_endpoints(text):
    return [{"method": m.upper(), "route": r} for m, r in _ROUTE.findall(text)]


def extract_tables(text):
    return sorted(set(_TABLE.findall(text)))
