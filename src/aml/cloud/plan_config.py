"""Central tunable config for Cloud plan usage DISPLAY (Metering Phase 2).

Single home for the two display tunables — the per-plan included governed-decision
allotment and the "approaching" warning threshold.

HARD SCOPE: display only. These values are PLACEHOLDERS, NOT final pricing, and NOTHING
in the codebase gates, denies, or throttles a decision based on them. There is no hard
ceiling in Phase 1/2 — a Free tenant over its allotment keeps working. Real allotments,
real pricing, and any enforcement/ceiling are later, separately-attested steps (Phase 3+).
"""
from __future__ import annotations

# Included governed-decision allotment per plan (DISPLAY ONLY). ``None`` ⇒ no numeric
# allotment shown (enterprise / custom). Keys are the real stored ``tenants.plan`` values.
INCLUDED_ALLOTMENT: dict[str, int | None] = {
    "starter": 10_000,
    "pro": 100_000,
    "enterprise": None,
}

# Fraction of the allotment at which the console shows an "approaching" nudge (Phase 2).
WARN_PCT: float = 0.80

# These allotment figures are placeholders in Phase 1/2.
INCLUDED_ALLOTMENT_IS_PLACEHOLDER: bool = True
