"""The installed package version — the single source the server reports (e.g. GET /health)."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def package_version() -> str:
    """`importlib.metadata.version("grafomem")`; "0+unknown" when running from a source tree that is not installed."""
    try:
        return version("grafomem")
    except PackageNotFoundError:
        return "0+unknown"
