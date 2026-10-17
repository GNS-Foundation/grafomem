"""The Free-tier ceiling exception, importable without the PostgreSQL client.

The app registers its HTTP 402 handler for this exception in every mode, including `grafomem serve -b sqlite`, so it
lives apart from `aml.cloud.free_ceiling` (which needs psycopg); that module re-exports it unchanged.
"""
from __future__ import annotations


class FreeCeilingExceeded(Exception):
    """Raised when a free-tier tenant is over its governed-decision ceiling.

    Carries an upgrade-oriented message; translated to HTTP 402 by the app's handler.
    """

    def __init__(self, tenant_id: str, count: int, ceiling: int) -> None:
        self.tenant_id = tenant_id
        self.count = count
        self.ceiling = ceiling
        super().__init__(
            f"Free plan governed-decision ceiling reached ({count} ≥ {ceiling} this period). "
            f"Upgrade to Pro to keep making governed decisions."
        )
