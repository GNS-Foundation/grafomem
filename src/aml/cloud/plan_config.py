"""Central tunable config for Cloud plan usage + the free-tier ceiling.

Home for the per-plan included governed-decision allotment, the "approaching" warning
threshold, and the free-tier hard-ceiling tunables.

Metering Phase 3e (2026-08-20): the values below are the **confirmed launch defaults**
(signed off by Camilo, 2026-08-20; tunable post-launch), no longer placeholders.

SCOPE UNCHANGED — setting these numbers ARMS NOTHING. Nothing in the codebase gates,
denies, or bills based on them until the cutover env-var flips (``FREE_CEILING_ENABLED``
for the ceiling; the live Stripe price ids for metered billing). Until then the ceiling
and metering stay dark and the console shows usage as indicative
(``INCLUDED_ALLOTMENT_IS_PLACEHOLDER`` stays True). This module only records intent.
"""
from __future__ import annotations

# Included governed-decision allotment per plan (DISPLAY + free-ceiling reference).
# ``None`` ⇒ no numeric allotment (enterprise / custom). Keys are the real stored
# ``tenants.plan`` values. CONFIRMED LAUNCH DEFAULTS (Camilo 2026-08-20; tunable).
INCLUDED_ALLOTMENT: dict[str, int | None] = {
    "starter": 10_000,      # free/entry tier included
    "pro": 100_000,         # Pro included; MUST equal the Stripe overage tier-1 boundary (see below)
    "enterprise": None,     # custom — no numeric allotment, no ceiling, no divide
}

# Fraction of the allotment at which the console shows an "approaching" nudge (Phase 2).
WARN_PCT: float = 0.80

# The console still labels the displayed allotment as indicative until the attest-gate
# cutover arms enforcement/billing. The NUMBERS are confirmed (2026-08-20); this flag
# governs DISPLAY wording only and intentionally stays True until cutover — do NOT flip
# it in 3e.
INCLUDED_ALLOTMENT_IS_PLACEHOLDER: bool = True


# ── Free (starter) hard ceiling (DARK; enforced by free_ceiling.py) ──────────────────
# The plan value that counts as the free/entry tier. There is no literal ``free`` tier
# in the data model — ``starter`` is the free/entry tier (the Phase-2 console nudges
# ``starter`` → Pro). Only this plan is ever blocked; pro/enterprise/unknown/NULL never.
FREE_PLAN: str = "starter"

# Hard ceiling on governed decisions per period for the free tier. CONFIRMED LAUNCH
# DEFAULT (Camilo 2026-08-20; tunable). Enforced ONLY when FREE_CEILING_ENABLED (default
# off) and only for FREE_PLAN tenants, and only after an authoritative plan re-check at
# the boundary.
FREE_CEILING: int = 50_000

# Background refresh cadence for free_usage_cache, and the staleness horizon beyond
# which a cached row is treated as unusable → fail-open (allow). Kept well above the
# refresh cadence so normal in-flight refresh is never mistaken for staleness.
FREE_CEILING_REFRESH_MIN: int = 10
FREE_CEILING_STALE_SEC: int = 1800  # 30 min (3× refresh) → stale row never blocks


# ── Stripe-side billing reference (DOCUMENTATION ONLY — does not drive code) ──────────
# The billing AUTHORITY is the Stripe price object, created in LIVE mode at cutover. These
# constants document the confirmed intent (Camilo 2026-08-20); code never bills from them.
#
# Pro subscription at cutover = two items:
#   • base:    flat $PRO_BASE_USD / month (licensed)
#   • overage: graduated metered price on the governed-decisions sum-meter —
#                tier 1: up_to STRIPE_OVERAGE_TIER1_UP_TO  @ $0        (included)
#                tier 2: up_to inf                          @ $PRO_OVERAGE_PER_DECISION_USD / decision
# The 3a TEST-mode instantiation of this shape is overage price
# ``price_1U6SyD2WoiKDsPejXbyoumFH`` (tier-1 up_to = 100,000); the LIVE price created at
# cutover MUST satisfy the same tier-1 boundary invariant below.
PRO_BASE_USD: float = 20.0                    # Pro flat base, monthly
PRO_OVERAGE_PER_DECISION_USD: float = 0.001   # $1 per 1,000 decisions over the included amount
STRIPE_OVERAGE_TIER1_UP_TO: int = 100_000     # Stripe overage tier-1 boundary (= Pro included)

# INVARIANT: the Pro included allotment shown/counted here must equal the Stripe overage
# price's tier-1 ``up_to`` boundary, or the console's "included" number and the point at
# which paid overage begins would disagree. Enforced at import for the config pair; the
# LIVE Stripe price must be created to satisfy the same equality at cutover.
assert INCLUDED_ALLOTMENT["pro"] == STRIPE_OVERAGE_TIER1_UP_TO, (
    "Pro included allotment must equal the Stripe overage tier-1 boundary "
    f"({INCLUDED_ALLOTMENT['pro']} != {STRIPE_OVERAGE_TIER1_UP_TO})"
)
