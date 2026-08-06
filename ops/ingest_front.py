#!/usr/bin/env python3
"""ops/ingest_front.py — Phase-0 GTM-front ingestion adapter for the Ulissy tenant.

Turns a GTM outreach ledger (CSV or JSON export) into governed primitives on the
Ulissy tenant, reusing the existing invoice-shaped substrate via **semantic mapping
(Design Decision A)** — zero schema change:

    ledger row  ->  DECIDE   POST /v1/governed/decisions   (one per row w/ a decision)
    resolved    ->  RESOLVE  POST /v1/governed/outcomes/bulk
    reviewed    ->  REVIEW   POST /v1/governed/reviews/bulk

    ref               -> invoice_id            (server aliases to invoice_ref, the CGR join key)
    every outreach    -> decision "certify"    (the agent certifies this is a sound outreach call)
    meeting_booked    -> outcome "paid"
    passed/no_response-> outcome "default"
    replied/sent/...  -> interim, NO outcome yet (see STATUS_TO_OUTCOME / roadmap item B)

The scored subject is the front-agent (default `gtm-outreach-agent@ulissy`), carried on
each decision as `agent_handle` (display label) + a **stable** `agent_key` (grouping key,
pinned once in the creds file — see setup_ulissy_tenant.py). If the key ever changes
between runs the agent's score fragments, so it is read from creds, never regenerated here.

Idempotency / re-runnability (safe to run daily/weekly):
  * Decisions have NO server-side dedup — a local manifest (.ulissy_ingest_state.json) of
    already-posted refs prevents re-runs from double-counting and corrupting the score.
  * Outcomes are latest-wins per ref; reviews dedup on (ref, reviewer) — both server-side
    idempotent, so they are always re-posted (cheap, chunked).

GUARDRAIL (from the charter, non-negotiable): this adapter ONLY records decisions/
outcomes. It never sends email or contacts anyone — there is no send path in this file.
Every outreach to a named human stays a founder-approved edge action, out of band.

Usage:
    python ops/ingest_front.py ops/sample_ledger.csv            # ingest (writes to BASE)
    python ops/ingest_front.py ops/sample_ledger.csv --dry-run  # preview, no POST
    GRAFOMEM_BASE=http://localhost:8090 python ops/ingest_front.py path.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Any, Iterable, Iterator

# ---------------------------------------------------------------------------
# Pure mapping layer (no HTTP, no I/O) — this is what the unit tests exercise.
# ---------------------------------------------------------------------------

DEFAULT_AGENT = "gtm-outreach-agent@ulissy"

# Semantic mapping (A): GTM resolve status -> governed outcome.
# Only TERMINAL statuses emit an outcome. Interim statuses (proposed/sent/opened/
# replied) intentionally emit nothing — the outreach hasn't resolved, and the
# invoice-shaped outcome set {paid, default, disputed, late, written_off} has no
# "engaged/interim" member. Representing that faithfully needs generic outcome
# types (ROADMAP item B). Keys are matched case-insensitively after slugifying.
STATUS_TO_OUTCOME: dict[str, str] = {
    "meeting_booked": "paid",
    "meeting-booked": "paid",
    "meeting": "paid",
    "booked": "paid",
    "call_booked": "paid",
    "passed": "default",
    "no_response": "default",
    "no-response": "default",
    "noresponse": "default",
    "bounced": "default",
    "declined": "default",
    "unsubscribed": "default",
    "not_interested": "default",
}

# Interim / not-yet-resolved statuses: recognised, but emit no outcome.
INTERIM_STATUSES: frozenset[str] = frozenset(
    {"", "proposed", "sent", "queued", "opened", "clicked", "replied", "in_progress", "pending"}
)


def slugify(s: str) -> str:
    """Lowercase, non-alphanumerics -> single dashes, trimmed. Stable & URL-safe."""
    out = []
    prev_dash = False
    for ch in (s or "").strip().lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")


def _norm_status(row: dict) -> str:
    return slugify(str(row.get("status", "") or "")).replace("-", "_")


def make_ref(row: dict) -> str:
    """Deterministic ref for a ledger row. Explicit `ref` column wins; else
    OUT-<company>-<person> per the charter's mapping. Deterministic so re-exports
    of the same row map to the same governed ref (idempotency depends on this)."""
    explicit = str(row.get("ref", "") or "").strip()
    if explicit:
        return explicit
    company = slugify(str(row.get("company", "") or ""))
    person = slugify(str(row.get("person", "") or ""))
    parts = [p for p in (company, person) if p]
    if not parts:
        raise ValueError(f"row has no ref and no company/person to derive one: {row!r}")
    return "OUT-" + "-".join(parts)


def row_to_decision(row: dict, agent_handle: str, agent_key: str,
                    agent_tier: float | None = None) -> dict | None:
    """Map a ledger row -> a governed decision body, or None if the row records no
    outreach decision. Semantic mapping (A): every recorded outreach is a "certify"."""
    ref = make_ref(row)
    reason = str(row.get("rationale", row.get("reason", "")) or "").strip()
    # Rationale/context is carried in `context` (per-tenant encrypted at rest server-side).
    context = {
        "outreach_ref": ref,
        "company": str(row.get("company", "") or ""),
        "person": str(row.get("person", "") or ""),
        "channel": str(row.get("channel", "") or ""),
        "message_variant": str(row.get("message_variant", row.get("variant", "")) or ""),
        "dimension": "gtm-outreach",
        # The edge is load-bearing: this record is a PROPOSAL, never an executed send.
        "edge_gate": True,
        "executed": False,
        "edge_approved": _as_bool(row.get("edge_approved")),
    }
    body: dict[str, Any] = {
        "decision": "certify",
        "reason": reason or "outreach proposed",
        "invoice_id": ref,
        "context": context,
        "agent_handle": agent_handle,
        "verifiability_tag": "judgment",
        "agent_key": agent_key,
    }
    tier = _as_float(row.get("agent_tier")) if row.get("agent_tier") is not None else agent_tier
    if tier is not None:
        body["agent_tier"] = tier
    return body


def row_to_outcome(row: dict) -> dict | None:
    """Map a resolved ledger row -> an outcome event, or None if unresolved/interim."""
    status = _norm_status(row)
    if status in STATUS_TO_OUTCOME:
        out: dict[str, Any] = {
            "invoice_ref": make_ref(row),
            "outcome": STATUS_TO_OUTCOME[status],
            "source": "gtm_ledger",
        }
        rdate = str(row.get("resolved_date", row.get("outcome_date", "")) or "").strip()
        if rdate:
            out["outcome_date"] = rdate
        return out
    return None  # interim or unknown -> no outcome


def row_to_review(row: dict) -> dict | None:
    """Map a ledger row -> a review, or None. In Phase 0 the founder's edge-approval is
    the natural first reviewer signal (reviewer_handle + rating 0..1)."""
    reviewer = str(row.get("reviewer", row.get("reviewer_handle", "")) or "").strip()
    rating = _as_float(row.get("rating"))
    if not reviewer or rating is None:
        return None
    rating = max(0.0, min(1.0, rating))
    return {
        "invoice_ref": make_ref(row),
        "reviewer_handle": reviewer,
        "rating": round(rating, 3),
        "source": str(row.get("review_source", "founder_edge") or "founder_edge"),
    }


def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in {"1", "true", "yes", "y", "t", "approved"}


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def unknown_statuses(rows: Iterable[dict]) -> list[str]:
    """Statuses that are neither a known outcome nor a known interim state — surfaced
    so a silent typo in the ledger never masquerades as 'nothing resolved'."""
    seen: set[str] = set()
    for r in rows:
        s = _norm_status(r)
        if s not in STATUS_TO_OUTCOME and s not in INTERIM_STATUSES:
            seen.add(s)
    return sorted(seen)


# ---------------------------------------------------------------------------
# I/O layer — load ledger, idempotency manifest, HTTP posting.
# ---------------------------------------------------------------------------

def load_rows(path: str) -> list[dict]:
    """Read a ledger export. .json -> list[dict] (or {rows|records|data: [...]});
    anything else -> CSV with a header row."""
    if path.lower().endswith(".json"):
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in ("rows", "records", "data", "ledger"):
                if isinstance(data.get(k), list):
                    return data[k]
            raise ValueError(f"{path}: JSON object has no rows/records/data list")
        if isinstance(data, list):
            return data
        raise ValueError(f"{path}: unsupported JSON shape {type(data).__name__}")
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _chunks(seq: list, n: int) -> Iterator[list]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class IngestState:
    """Local manifest of already-posted decision refs — the client-side idempotency
    that /v1/governed/decisions (no server dedup) requires for safe re-runs."""

    def __init__(self, path: str):
        self.path = path
        self.posted: set[str] = set()
        if os.path.exists(path):
            with open(path) as f:
                self.posted = set(json.load(f).get("posted_decisions", []))

    def has(self, ref: str) -> bool:
        return ref in self.posted

    def add(self, ref: str) -> None:
        self.posted.add(ref)

    def save(self) -> None:
        with open(self.path, "w") as f:
            json.dump({"posted_decisions": sorted(self.posted)}, f, indent=2)
        os.chmod(self.path, 0o600)


def _state_path() -> str:
    return os.environ.get(
        "ULISSY_INGEST_STATE",
        os.path.join(os.path.dirname(__file__), ".ulissy_ingest_state.json"),
    )


def run(path: str, *, dry_run: bool = False, agent_handle: str = DEFAULT_AGENT) -> dict:
    """Ingest `path` onto the Ulissy tenant. Returns a summary dict."""
    import common  # local import: keeps mapping layer HTTP-free
    from common import BASE, client, load_creds

    # Dry-run may preview the mapping before the tenant exists — use a placeholder key.
    creds = None
    if dry_run and not os.path.exists(common.CREDS_PATH):
        print("  (dry-run without creds — using placeholder agent_key)")
        acfg: dict = {}
    else:
        creds = load_creds()
        acfg = creds.get("agents", {}).get(agent_handle) or {}
    agent_key = acfg.get("agent_key") or ("0" * 64 if dry_run else None)
    if not agent_key:
        sys.exit(f"blocked — no pinned agent_key for {agent_handle} in creds. "
                 f"Run: python ops/setup_ulissy_tenant.py")
    agent_tier = acfg.get("agent_tier")

    rows = load_rows(path)
    unknown = unknown_statuses(rows)
    if unknown:
        print(f"  ⚠️  unknown statuses in ledger (no outcome emitted, check for typos): {unknown}")

    state = IngestState(_state_path())

    # Build the three governed payloads via the pure mapping layer.
    new_decisions: list[tuple[str, dict]] = []
    for r in rows:
        body = row_to_decision(r, agent_handle, agent_key, agent_tier)
        if body is None:
            continue
        ref = body["invoice_id"]
        if state.has(ref):
            continue  # already posted a decision for this ref — do not double-count
        new_decisions.append((ref, body))

    outcomes = [o for r in rows if (o := row_to_outcome(r)) is not None]
    reviews = [rv for r in rows if (rv := row_to_review(r)) is not None]

    print(f"  BASE = {BASE}")
    print(f"  ledger rows: {len(rows)}  |  new decisions: {len(new_decisions)}  "
          f"(skipped {sum(1 for r in rows if row_to_decision(r, agent_handle, agent_key) and state.has(make_ref(r)))} already-posted)  "
          f"|  outcomes: {len(outcomes)}  |  reviews: {len(reviews)}")

    if dry_run:
        print("  --dry-run: no POSTs made. Sample decision:")
        if new_decisions:
            print("   ", json.dumps(new_decisions[0][1], indent=2)[:600])
        return {"dry_run": True, "new_decisions": len(new_decisions),
                "outcomes": len(outcomes), "reviews": len(reviews), "unknown_statuses": unknown}

    posted_dec = 0
    with client(creds["api_key"], timeout=90.0) as c:
        # DECIDE — one POST per decision (no bulk endpoint); record ref only on success.
        for ref, body in new_decisions:
            r = c.post("/v1/governed/decisions", json=body)
            r.raise_for_status()
            state.add(ref)
            posted_dec += 1
        state.save()  # persist manifest even if a later stage fails

        # RESOLVE — chunked bulk (server idempotent: latest-wins per ref).
        rec_out = 0
        for ch in _chunks(outcomes, 8):
            r = c.post("/v1/governed/outcomes/bulk", json=ch)
            r.raise_for_status()
            rec_out += r.json().get("count", len(ch))

        # REVIEW — chunked bulk (server idempotent: dedup on (ref, reviewer)).
        rec_rev = 0
        for ch in _chunks(reviews, 20):
            r = c.post("/v1/governed/reviews/bulk", json=ch)
            r.raise_for_status()
            rec_rev += r.json().get("count", len(ch))

    print(f"  ✅ posted: decisions={posted_dec}  outcomes={rec_out}  reviews={rec_rev}")
    return {"dry_run": False, "posted_decisions": posted_dec, "outcomes": rec_out,
            "reviews": rec_rev, "unknown_statuses": unknown}


def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest a GTM ledger onto the Ulissy tenant (Phase 0).")
    ap.add_argument("ledger", help="path to the ledger export (.csv or .json)")
    ap.add_argument("--dry-run", action="store_true", help="preview mapping, make no POSTs")
    ap.add_argument("--agent", default=DEFAULT_AGENT, help=f"front-agent handle (default {DEFAULT_AGENT})")
    args = ap.parse_args()
    # Ensure the sibling `common` module is importable when run as a script.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    run(args.ledger, dry_run=args.dry_run, agent_handle=args.agent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
