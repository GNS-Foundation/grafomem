#!/usr/bin/env python3
"""Create the GRAFOMEM Pro Stripe objects — byte-identical in config to the 3a TEST objects.

Go-live Stage 1, Task 3. Staged for Stage 2 (Camilo's attest, live key). This script does
NOT run against live by accident:

  * Default is TEST mode + DRY-RUN (prints the config, creates nothing).
  * ``--create`` actually creates. ``--live`` targets live mode (else test).
  * Mode is resolved from the key PREFIX and printed before anything is created; the script
    ABORTS if the key doesn't match the requested mode (a test key under ``--live``, or a
    live key without ``--live``).
  * Creating in LIVE (``--create --live``) additionally requires ``--confirm-live``.

Object shape (matches the 3a test objects; numbers derive from plan_config so they can't
drift from the locked launch defaults):
  * Product   "GRAFOMEM Pro"
  * Meter     event ``governed_decisions``, default_aggregation sum
  * Base      $PRO_BASE_USD / month, licensed
  * Overage   recurring metered, graduated:
                tier1 up_to STRIPE_OVERAGE_TIER1_UP_TO @ $0
                tier2 up_to inf @ PRO_OVERAGE_PER_DECISION_USD (via unit_amount_decimal)

Keys (env): TEST → STRIPE_TEST_SECRET_KEY; LIVE → STRIPE_LIVE_SECRET_KEY (falls back to
STRIPE_SECRET_KEY). Do NOT run --create --live outside the attested cutover.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Numbers come from the single source of truth (Phase 3e locked defaults).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from aml.cloud.plan_config import (  # noqa: E402
    PRO_BASE_USD,
    PRO_OVERAGE_PER_DECISION_USD,
    STRIPE_OVERAGE_TIER1_UP_TO,
)

PRODUCT_NAME = "GRAFOMEM Pro"
METER_EVENT_NAME = "governed_decisions"
BASE_UNIT_AMOUNT = int(round(PRO_BASE_USD * 100))                 # $20.00 → 2000 cents
OVERAGE_TIER2_DECIMAL = str(PRO_OVERAGE_PER_DECISION_USD * 100)   # $0.001/unit → "0.1" cents


def _specs(meter_id_placeholder: str = "<meter.id>") -> dict:
    """The exact create() kwargs for each object (meter id filled in after the meter is made)."""
    return {
        "product": {"name": PRODUCT_NAME},
        "meter": {
            "display_name": "Governed decisions",
            "event_name": METER_EVENT_NAME,
            "default_aggregation": {"formula": "sum"},
            "customer_mapping": {"type": "by_id", "event_payload_key": "stripe_customer_id"},
            "value_settings": {"event_payload_key": "value"},
        },
        "base_price": {
            "currency": "usd",
            "unit_amount": BASE_UNIT_AMOUNT,
            "recurring": {"interval": "month", "usage_type": "licensed"},
            "nickname": "GRAFOMEM Pro base $20/mo",
        },
        "overage_price": {
            "currency": "usd",
            "billing_scheme": "tiered",
            "tiers_mode": "graduated",
            "tiers": [
                {"up_to": STRIPE_OVERAGE_TIER1_UP_TO, "unit_amount_decimal": "0"},
                {"up_to": "inf", "unit_amount_decimal": OVERAGE_TIER2_DECIMAL},
            ],
            "recurring": {"interval": "month", "usage_type": "metered", "meter": meter_id_placeholder},
            "nickname": "GRAFOMEM Pro overage $0.001/decision above 100k",
        },
    }


def _resolve_key(live: bool) -> str:
    """Return the key for the requested mode, ABORTING if its prefix disagrees."""
    if live:
        key = (os.environ.get("STRIPE_LIVE_SECRET_KEY") or os.environ.get("STRIPE_SECRET_KEY") or "").strip()
        if not key:
            sys.exit("ABORT: --live requested but no STRIPE_LIVE_SECRET_KEY / STRIPE_SECRET_KEY set")
        if not key.startswith(("sk_live_", "rk_live_")):
            sys.exit(f"ABORT: --live requested but key is not a live key (prefix {key[:8]!r}) — refusing")
        return key
    key = (os.environ.get("STRIPE_TEST_SECRET_KEY") or "").strip()
    if not key:
        sys.exit("ABORT: no STRIPE_TEST_SECRET_KEY set (test mode is the default)")
    if not key.startswith(("sk_test_", "rk_test_")):
        sys.exit(f"ABORT: test mode requested but key is not a test key (prefix {key[:8]!r}) — refusing")
    return key


def main() -> None:
    ap = argparse.ArgumentParser(description="Create GRAFOMEM Pro Stripe objects (guarded).")
    ap.add_argument("--live", action="store_true", help="Target LIVE mode (default: test).")
    ap.add_argument("--create", action="store_true", help="Actually create (default: dry-run).")
    ap.add_argument("--confirm-live", action="store_true", help="Required with --create --live.")
    args = ap.parse_args()

    mode = "LIVE" if args.live else "TEST"
    key = _resolve_key(args.live)
    print(f"RESOLVED MODE: {mode}  (key prefix {key[:8]}…)")
    print(f"Config source: plan_config — base ${PRO_BASE_USD}/mo, overage ${PRO_OVERAGE_PER_DECISION_USD}"
          f"/decision, tier-1 up_to {STRIPE_OVERAGE_TIER1_UP_TO}")

    specs = _specs()
    if not args.create:
        print("\nDRY-RUN — would create (no Stripe calls):")
        print(json.dumps(specs, indent=2))
        print("\n(Pass --create to create; --live to target live mode.)")
        return

    if args.live and not args.confirm_live:
        sys.exit("ABORT: --create --live requires --confirm-live (deliberate live write).")

    import stripe
    stripe.api_key = key
    print(f"\nCreating objects in {mode} mode…")

    product = stripe.Product.create(**specs["product"])
    print("product:", product.id)
    meter = stripe.billing.Meter.create(**specs["meter"])
    print("meter:", meter.id, "| event:", meter.event_name)
    base = stripe.Price.create(product=product.id, **specs["base_price"])
    print("base_price:", base.id)
    ov = dict(specs["overage_price"])
    ov["recurring"] = {**ov["recurring"], "meter": meter.id}
    overage = stripe.Price.create(product=product.id, **ov)
    print("overage_price:", overage.id)

    print("\nCreated:", json.dumps({
        "mode": mode.lower(), "product": product.id, "meter": meter.id,
        "base_price": base.id, "overage_price": overage.id,
    }, indent=2))
    if args.live:
        print("\n⚠ LIVE objects created. Set STRIPE_METER_ID / STRIPE_BASE_PRICE_ID / "
              "STRIPE_OVERAGE_PRICE_ID to these ids to arm metering.")


if __name__ == "__main__":
    main()
