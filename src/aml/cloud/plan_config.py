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


# ── Metering Phase 3c — Free (starter) hard ceiling (DARK; see free_ceiling.py) ──
# The plan value that counts as the free/entry tier. There is no literal ``free`` tier
# in the data model — ``starter`` is the free/entry tier (the Phase-2 console nudges
# ``starter`` → Pro). Only this plan is ever blocked; pro/enterprise/unknown/NULL never.
FREE_PLAN: str = "starter"

# Hard ceiling on governed decisions per period for the free tier. PLACEHOLDER — not
# final pricing. Enforced ONLY when FREE_CEILING_ENABLED (default off) and only for
# FREE_PLAN tenants, and only after an authoritative plan re-check at the boundary.
FREE_CEILING: int = 50_000

# Background refresh cadence for free_usage_cache, and the staleness horizon beyond
# which a cached row is treated as unusable → fail-open (allow). Kept well above the
# refresh cadence so normal in-flight refresh is never mistaken for staleness.
FREE_CEILING_REFRESH_MIN: int = 10
FREE_CEILING_STALE_SEC: int = 1800  # 30 min (3× refresh) → stale row never blocks
