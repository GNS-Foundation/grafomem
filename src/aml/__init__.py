"""GRAFOMEM — agent-memory benchmark framework."""

# Single source of truth is pyproject.toml (the installed package metadata); this
# literal had drifted to "0.1.3" while the package was at 0.4.2. Derive it instead.
from aml._version import package_version as _package_version

__version__ = _package_version()
