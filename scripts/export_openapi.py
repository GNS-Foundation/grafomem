#!/usr/bin/env python3
"""
Export the GRAFOMEM OpenAPI specification to a JSON file.

Usage:
    python scripts/export_openapi.py                 # writes to docs/openapi.json
    python scripts/export_openapi.py --output api.json

CI usage (drift detection):
    python scripts/export_openapi.py
    diff docs/openapi.json docs/openapi.json.snapshot
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure project root is importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Export GRAFOMEM OpenAPI spec")
    parser.add_argument(
        "--output", "-o",
        default=str(ROOT / "docs" / "openapi.json"),
        help="Output file path (default: docs/openapi.json)",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indent (default: 2)",
    )
    args = parser.parse_args()

    # Boot the app with a dummy DB URL to enable all cloud routes
    # (we're only reading the schema, not connecting)
    from aml.server.app import create_app

    app = create_app(
        db_url="postgresql://export:export@localhost:5432/export",
        auth_mode="none",
    )

    # FastAPI generates the OpenAPI spec lazily
    spec = app.openapi()

    # Summary stats
    paths = spec.get("paths", {})
    schemas = spec.get("components", {}).get("schemas", {})
    endpoint_count = sum(len(methods) for methods in paths.values())

    # Write
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        json.dump(spec, f, indent=args.indent, sort_keys=False, ensure_ascii=False)

    print(f"✅ OpenAPI spec exported to: {out}")
    print(f"   Version:   {spec.get('info', {}).get('version', '?')}")
    print(f"   Paths:     {len(paths)}")
    print(f"   Endpoints: {endpoint_count}")
    print(f"   Schemas:   {len(schemas)}")

    # Warn about endpoints missing response schemas
    missing = []
    for path, methods in paths.items():
        for method, detail in methods.items():
            if method in ("get", "post", "put", "delete", "patch"):
                responses = detail.get("responses", {})
                for code, resp_detail in responses.items():
                    if code.startswith("2") and "content" not in resp_detail:
                        missing.append(f"  {method.upper()} {path}")
                        break

    if missing:
        print(f"\n⚠️  {len(missing)} endpoints missing response schemas:")
        for m in missing[:15]:
            print(f"   {m}")
        if len(missing) > 15:
            print(f"   ... and {len(missing) - 15} more")
    else:
        print("\n✅ All endpoints have response schemas")


if __name__ == "__main__":
    main()
