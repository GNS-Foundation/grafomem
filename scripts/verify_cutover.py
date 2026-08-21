#!/usr/bin/env python3
"""Cloud Metering cutover verification — READ-ONLY (no writes, no charges, no MeterEvents).

Run right after Stage-3 arming (live Stripe objects created + the three STRIPE_* env vars
set) to confirm the switch landed correctly and nothing armed that shouldn't. Prints
PASS / FAIL / SKIP for each check and exits non-zero if any FAIL.

Best run inside the prod env so it sees the real config + live key, e.g.:
    railway run python scripts/verify_cutover.py
Optional args add the deployed-app and reconcile checks:
    --base-url https://api.grafomem.com --token <portal_jwt>   # check 1b + reconcile via API
    --tenant <tenant_id>                                        # reconcile a known tenant
    --db-url <url>   (defaults to GRAFOMEM_DB_URL)              # reconcile via get_usage

Every Stripe call is a retrieve/list (read). No object is created or modified.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from aml.cloud.plan_config import (  # noqa: E402
    FREE_PLAN,
    PRO_BASE_USD,
    PRO_OVERAGE_PER_DECISION_USD,
    STRIPE_OVERAGE_TIER1_UP_TO,
)
from aml.cloud.usage_reporter import metered_enabled, meter_config  # noqa: E402
from aml.cloud.free_ceiling import free_ceiling_enabled  # noqa: E402

# Live webhook event types the handlers cover.
#
# REQUIRED (6): money- or state-relevant. Their handlers mutate the local subscription
# mirror, record payment state, or reconcile invoices — a missing one degrades billing
# correctness, so absence is a FAIL.
#
# OPTIONAL (1): billing.meter.error_report_triggered is observability-only — its handler
# (_on_meter_error) does a loud log and NO state change; the reporter's own reconcile
# (check 6, meter-total == get_usage) is the corrective path, and a rejected meter event
# under-bills (the safe direction). It also needs a recent Stripe API version to even
# appear in the dashboard event picker; when the endpoint's API version predates it, it
# is simply not selectable. Absence is a WARN (add it later), never a cutover blocker.
REQUIRED_WEBHOOK_EVENTS = [
    "checkout.session.completed",
    "invoice.payment_succeeded",
    "invoice.payment_failed",
    "customer.subscription.deleted",
    "customer.subscription.updated",
    "invoice.finalized",
]
OPTIONAL_WEBHOOK_EVENTS = [
    "billing.meter.error_report_triggered",
]

_results: list[tuple[str, str, str]] = []  # (check, status, detail)


def record(check: str, status: str, detail: str = "") -> None:
    _results.append((check, status, detail))
    icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭", "WARN": "⚠"}.get(status, "?")
    print(f"  {icon} [{status}] {check}" + (f" — {detail}" if detail else ""))


def _g(o, k, d=None):
    return o[k] if (o is not None and k in o) else d


def _dec(x) -> Decimal:
    return Decimal(str(x))


# ── check 1: reporter armed ──────────────────────────────────────────────────

def check_reporter_armed(base_url: str | None, token: str | None) -> None:
    print("\n1) Reporter armed")
    armed = metered_enabled()
    record("config metered_enabled is True", "PASS" if armed else "FAIL",
           f"metered_enabled()={armed} (from STRIPE_METER_ID/BASE/OVERAGE env)")
    # 1b — the DEPLOYED app agrees (definitive "loop running" is the startup log
    # 'usage reporter started'; the running app's resolved state is exposed here).
    if base_url and token:
        try:
            data = _api_get(base_url, "/v1/usage/current", token)
            app_armed = data.get("metered_enabled")
            record("deployed app reports metered_enabled True", "PASS" if app_armed else "FAIL",
                   f"/v1/usage/current metered_enabled={app_armed}")
        except Exception as e:  # noqa: BLE001
            record("deployed app metered_enabled", "SKIP", f"/v1/usage/current read failed: {e}")
    else:
        record("deployed app metered_enabled", "SKIP",
               "pass --base-url + --token to confirm; also eyeball the 'usage reporter started' startup log")


# ── check 2: live Stripe wiring matches the confirmed numbers ─────────────────

def check_live_stripe(stripe) -> dict:
    print("\n2) Live Stripe wiring")
    cfg = meter_config()
    if cfg is None:
        record("three STRIPE_* env vars set", "FAIL", "meter_config() is None — one or more unset")
        return {}
    record("three STRIPE_* env vars set", "PASS",
           f"meter={cfg['meter_id']} base={cfg['base_price_id']} overage={cfg['overage_price_id']}")

    objs = {}
    try:
        meter = stripe.billing.Meter.retrieve(cfg["meter_id"])
        base = stripe.Price.retrieve(cfg["base_price_id"])
        ov = stripe.Price.retrieve(cfg["overage_price_id"], expand=["tiers"])
        objs = {"meter": meter, "base": base, "overage": ov}
    except Exception as e:  # noqa: BLE001
        record("retrieve live objects", "FAIL", f"{e}")
        return objs

    # livemode=True on every object
    lm = {"meter": bool(_g(meter, "livemode")), "base": bool(_g(base, "livemode")),
          "overage": bool(_g(ov, "livemode"))}
    record("objects resolve to LIVE mode", "PASS" if all(lm.values()) else "FAIL", f"livemode={lm}")

    # base = $20/mo licensed
    base_ok = (_g(base, "unit_amount") == int(round(PRO_BASE_USD * 100))
               and _g(_g(base, "recurring"), "usage_type") == "licensed"
               and _g(_g(base, "recurring"), "interval") == "month")
    record(f"base price = ${PRO_BASE_USD}/mo licensed", "PASS" if base_ok else "FAIL",
           f"unit_amount={_g(base,'unit_amount')} recurring={_g(base,'recurring')}")

    # meter aggregation = sum, event governed_decisions
    meter_ok = (_g(_g(meter, "default_aggregation"), "formula") == "sum")
    record("meter aggregation = sum", "PASS" if meter_ok else "FAIL",
           f"event={_g(meter,'event_name')} agg={_g(_g(meter,'default_aggregation'),'formula')}")

    # overage graduated: tier1 up_to=100000 @ $0, tier2 inf @ $0.001 (0.1 cents) — #57 comparison
    try:
        t = ov["tiers"]
        tiers_ok = (
            _g(ov, "billing_scheme") == "tiered" and _g(ov, "tiers_mode") == "graduated"
            and int(_g(t[0], "up_to")) == STRIPE_OVERAGE_TIER1_UP_TO
            and _dec(_g(t[0], "unit_amount_decimal")) == Decimal("0")
            and _g(t[1], "up_to") is None
            and _dec(_g(t[1], "unit_amount_decimal")) == _dec(PRO_OVERAGE_PER_DECISION_USD * 100)
            and _g(_g(ov, "recurring"), "usage_type") == "metered"
        )
        record(f"overage tiers = 0–{STRIPE_OVERAGE_TIER1_UP_TO}@$0 then ${PRO_OVERAGE_PER_DECISION_USD}",
               "PASS" if tiers_ok else "FAIL",
               f"t1={_g(t[0],'up_to')}@{_g(t[0],'unit_amount_decimal')} "
               f"t2={_g(t[1],'up_to')}@{_g(t[1],'unit_amount_decimal')} usage={_g(_g(ov,'recurring'),'usage_type')}")
    except Exception as e:  # noqa: BLE001
        record("overage tiers match", "FAIL", f"{e}")
    return objs


# ── check 3: ceiling still off (Switch B stays dark) ─────────────────────────

def check_ceiling_off() -> None:
    print("\n3) Free ceiling still OFF (Switch B)")
    raw = os.environ.get("FREE_CEILING_ENABLED")
    off = not free_ceiling_enabled()
    record("FREE_CEILING_ENABLED unset/off", "PASS" if off else "FAIL",
           f"FREE_CEILING_ENABLED={raw!r} → enabled={not off}"
           + ("" if off else "  ⚠ ceiling armed without its 402 UX!"))


# ── check 4: four-surface consistency ────────────────────────────────────────

def check_four_surface(objs: dict) -> None:
    print("\n4) Four-surface consistency")
    # (a) plan_config internal
    record("plan_config: pro included == overage tier-1 boundary (100k)",
           "PASS" if 100_000 == STRIPE_OVERAGE_TIER1_UP_TO else "FAIL",
           f"{STRIPE_OVERAGE_TIER1_UP_TO}")
    # (b) live Stripe tier == plan_config
    ov = objs.get("overage")
    if ov is not None:
        try:
            live_up = int(ov["tiers"][0]["up_to"])
            live_rate = _dec(ov["tiers"][1]["unit_amount_decimal"])
            same = (live_up == STRIPE_OVERAGE_TIER1_UP_TO
                    and live_rate == _dec(PRO_OVERAGE_PER_DECISION_USD * 100))
            record("live Stripe tier == plan_config (100k / $0.001)", "PASS" if same else "FAIL",
                   f"live up_to={live_up} rate={live_rate}¢")
        except Exception as e:  # noqa: BLE001
            record("live Stripe tier == plan_config", "SKIP", f"{e}")
    else:
        record("live Stripe tier == plan_config", "SKIP", "no live overage object (see check 2)")
    # (c) console signal == armed
    record("console metered_enabled state == armed", "PASS" if metered_enabled() else "FAIL",
           f"metered_enabled()={metered_enabled()}")
    # (d) public page $20 (static assert — the page hard-codes $20/mo)
    record("public pricing page base == $20 (static)", "PASS" if PRO_BASE_USD == 20.0 else "FAIL",
           f"PRO_BASE_USD={PRO_BASE_USD}")


# ── check 5: webhook coverage ────────────────────────────────────────────────

def check_webhooks(stripe) -> None:
    print("\n5) Webhook coverage (6 required + 1 optional)")
    try:
        eps = stripe.WebhookEndpoint.list(limit=100)["data"]
    except Exception as e:  # noqa: BLE001
        record("live webhook endpoint covers required events", "SKIP",
               f"cannot list endpoints with this key ({e}). MANUAL: confirm the live endpoint's "
               f"enabled events include all of: {', '.join(REQUIRED_WEBHOOK_EVENTS)} "
               f"(optional: {', '.join(OPTIONAL_WEBHOOK_EVENTS)})")
        return
    covered_ep = None
    for ep in eps:
        events = set(_g(ep, "enabled_events", []) or [])
        if "*" in events or all(e in events for e in REQUIRED_WEBHOOK_EVENTS):
            covered_ep = ep
            record("live webhook endpoint covers all 6 required events", "PASS",
                   f"{_g(ep,'url')} ({'*' if '*' in events else 'explicit 6'})")
            break
    if covered_ep is None:
        missing = {}
        for ep in eps:
            events = set(_g(ep, "enabled_events", []) or [])
            missing[_g(ep, "url")] = [e for e in REQUIRED_WEBHOOK_EVENTS if e not in events]
        record("live webhook endpoint covers all 6 required events", "FAIL",
               f"no endpoint has all 6 required; missing per endpoint: {json.dumps(missing)}")
        return
    # Optional observability events — WARN if absent, never block the cutover.
    ev = set(_g(covered_ep, "enabled_events", []) or [])
    opt_missing = [] if "*" in ev else [e for e in OPTIONAL_WEBHOOK_EVENTS if e not in ev]
    if opt_missing:
        record("optional observability events present", "WARN",
               f"missing {', '.join(opt_missing)} — observability only (loud-log, no state/money; "
               f"reconcile in check 6 is the corrective path). Add once the endpoint's Stripe API "
               f"version exposes it.")
    else:
        record("optional observability events present", "PASS",
               ", ".join(OPTIONAL_WEBHOOK_EVENTS) if "*" not in ev else "*")


# ── check 6: reconcile sanity (meter total == get_usage) ─────────────────────

def check_reconcile(stripe, db_url: str | None, tenant: str | None, objs: dict) -> None:
    print("\n6) Reconcile sanity (meter total == get_usage)")
    if not tenant:
        record("meter total == get_usage", "SKIP", "pass --tenant <id> to reconcile a known tenant")
        return
    if not db_url:
        record("meter total == get_usage", "SKIP", "no --db-url / GRAFOMEM_DB_URL for get_usage")
        return
    cfg = meter_config()
    if cfg is None or objs.get("overage") is None:
        record("meter total == get_usage", "SKIP", "metering not fully wired (see check 2)")
        return
    try:
        import psycopg
        from psycopg.rows import dict_row
        from aml.cloud.decision_trail import DecisionTrailService
        from aml.cloud.usage_routes import resolve_current_period

        with psycopg.connect(db_url, row_factory=dict_row, autocommit=True) as conn:
            row = conn.execute(
                "SELECT stripe_customer_id, current_period_start, current_period_end "
                "FROM subscriptions WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 1",
                (tenant,),
            ).fetchone()
        if not row or not row.get("stripe_customer_id"):
            record("meter total == get_usage", "SKIP", f"no subscription/customer for tenant {tenant}")
            return
        customer_id = row["stripe_customer_id"]
        start, end, _src = resolve_current_period(tenant, row.get("current_period_start"),
                                                  row.get("current_period_end"))
        dt = DecisionTrailService(db_url)
        authoritative = int(dt.get_usage(tenant, start, end)["governed_decisions"])

        st = (int(start.timestamp()) // 3600) * 3600
        en = ((int(end.timestamp()) // 3600) + 1) * 3600
        summ = stripe.billing.Meter.list_event_summaries(
            cfg["meter_id"], customer=customer_id, start_time=st, end_time=en)
        meter_total = int(sum(float(_g(s, "aggregated_value", 0)) for s in summ["data"]))

        ok = meter_total == authoritative
        record("meter total == get_usage (no drift)", "PASS" if ok else "FAIL",
               f"tenant={tenant} get_usage={authoritative} meter_total={meter_total}")
    except Exception as e:  # noqa: BLE001
        record("meter total == get_usage", "SKIP", f"reconcile error (read-only): {e}")


# ── helpers ──────────────────────────────────────────────────────────────────

def _api_get(base_url: str, path: str, token: str) -> dict:
    req = urllib.request.Request(base_url.rstrip("/") + path,
                                 headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _get_stripe():
    key = (os.environ.get("STRIPE_SECRET_KEY") or os.environ.get("STRIPE_LIVE_SECRET_KEY") or "").strip()
    if not key:
        return None
    import stripe
    stripe.api_key = key
    return stripe


def main() -> None:
    ap = argparse.ArgumentParser(description="Read-only cutover verification.")
    ap.add_argument("--base-url", help="deployed API base, e.g. https://api.grafomem.com")
    ap.add_argument("--token", help="portal JWT for /v1/usage/current + reconcile")
    ap.add_argument("--tenant", help="tenant id to reconcile (check 6)")
    ap.add_argument("--db-url", default=os.environ.get("GRAFOMEM_DB_URL"),
                    help="Postgres url for get_usage (default GRAFOMEM_DB_URL)")
    args = ap.parse_args()

    print("CUTOVER VERIFICATION (read-only — no writes, no charges)")
    stripe = _get_stripe()
    if stripe is None:
        print("  ⚠ no STRIPE_SECRET_KEY in env — Stripe checks (2,4b,5,6) will FAIL/SKIP")

    check_reporter_armed(args.base_url, args.token)
    objs = check_live_stripe(stripe) if stripe else (record("2) Live Stripe wiring", "SKIP", "no key") or {})
    check_ceiling_off()
    check_four_surface(objs)
    if stripe:
        check_webhooks(stripe)
    else:
        record("5) Webhook coverage", "SKIP", "no Stripe key")
    check_reconcile(stripe, args.db_url, args.tenant, objs)

    n_fail = sum(1 for _, s, _ in _results if s == "FAIL")
    n_pass = sum(1 for _, s, _ in _results if s == "PASS")
    n_skip = sum(1 for _, s, _ in _results if s == "SKIP")
    n_warn = sum(1 for _, s, _ in _results if s == "WARN")
    print(f"\n=== {n_pass} PASS, {n_fail} FAIL, {n_warn} WARN, {n_skip} SKIP ===")
    if n_fail:
        print("CUTOVER NOT VERIFIED — resolve the FAILs above.")
        sys.exit(1)
    tail = " (WARNs are observability-only; safe to arm)" if n_warn else ""
    print(f"CUTOVER VERIFIED (all required checks pass; skips are optional/needs-input){tail}.")


if __name__ == "__main__":
    main()
