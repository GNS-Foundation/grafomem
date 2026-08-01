"""CGR-v1 validation — does the score predict default, and beat the naive baseline?

Two uses:
  * `synthetic_substrate()` — port of the reference generator
    (docs/cgr/cgr_substrate_reference.py) as real DecisionRow/ReviewEvent records,
    so the ported engine can be validated the same way the reference was.
  * `validate_report()` — corr(cgr_score, realized default rate) and the naive
    baseline corr(accept_rate, default), plus coverage stats. Works on synthetic
    OR live substrate rows.

CLI:  python -m aml.cgr.validate --synthetic
      python -m aml.cgr.validate --tenant <tenant_id>     # live (needs GRAFOMEM_DB_URL)

Imports: numpy + stdlib for the core; the live path lazy-imports the cloud
data-access classes (allowed — decision_trail/stores/backends), never portal/UI.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np

from aml.cgr.engine import compute_scores_from_rows
from aml.cgr.substrate import DecisionRow, ReviewEvent


@dataclass
class SyntheticData:
    rows: list[DecisionRow]
    reviews: list[ReviewEvent]
    truth_by_ref: dict[str, str]          # ALL certifies' realized outcomes (ground truth)
    handles: list[str]
    true_quality: dict[str, float]
    tier: dict[str, float]


def synthetic_substrate(seed: int = 7, n_agents: int = 12, n_inv: int = 4000,
                        n_rev: int = 20, resolved_fraction: float = 1.0,
                        with_tier: bool = True) -> SyntheticData:
    """Reproduce the reference's generative model as substrate records.

    Latent per-invoice default risk ~ Beta(2,4); an agent certifies iff its
    perceived risk < 0.5, where perception noise grows as true_quality falls; a
    certified invoice defaults with probability = its base risk. Better agents
    thus certify lower-risk portfolios that default less — the negative
    correlation CGR must recover. `resolved_fraction` < 1 withholds later
    outcomes from the scorer (early-signal simulation); `with_tier` controls
    whether agent_tier is populated (True mirrors the reference; False mirrors the
    current live path where TierGate is unwired and tier is None).
    """
    rng = np.random.default_rng(seed)
    true_quality = rng.uniform(0.35, 0.95, n_agents)
    tier = np.clip(true_quality + rng.normal(0, 0.08, n_agents), 0, 1)
    handles = [f"invoice-certifier-{i:02d}@kapwork-receivables" for i in range(n_agents)]
    rev_skill = rng.uniform(0.0, 1.0, n_rev)

    rows: list[DecisionRow] = []
    reviews: list[ReviewEvent] = []
    truth_by_ref: dict[str, str] = {}
    certified_refs: list[str] = []          # in generation order, for resolved_fraction slicing

    for k in range(n_inv):
        a = int(rng.integers(n_agents))
        ref = f"INV{k:05d}"
        amount = float(rng.uniform(1_000, 200_000))
        po_amount = amount * (1 + rng.normal(0, 0.05)) if rng.random() < 0.9 else amount * 1.5
        approval_present = rng.random() < 0.85
        duplicate = rng.random() < 0.04
        base_risk = rng.beta(2.0, 4.0)
        agent_tier = float(tier[a]) if with_tier else None

        rule_reject = (amount > po_amount * 1.05) or (not approval_present) or duplicate
        if rule_reject:
            reason = ("amount>PO" if amount > po_amount * 1.05
                      else ("no_debtor_approval" if not approval_present else "duplicate"))
            rows.append(DecisionRow(
                decision_id=f"dec-{k:05d}", invoice_ref=ref, agent_handle=handles[a],
                agent_tier=agent_tier, decision="reject", reason_code=reason,
                verifiability_tag="rule", created_at=None, outcome=None, outcome_date=None))
            continue

        perceived = float(np.clip(base_risk + rng.normal(0, 0.45 * (1 - true_quality[a]) + 0.03), 0, 1))
        certify = perceived < 0.50
        row = DecisionRow(
            decision_id=f"dec-{k:05d}", invoice_ref=ref, agent_handle=handles[a],
            agent_tier=agent_tier, decision="certify" if certify else "reject",
            reason_code="clean" if certify else "risk_judgment",
            verifiability_tag="judgment", created_at=None, outcome=None, outcome_date=None)
        rows.append(row)
        if certify:
            outcome = "default" if (rng.random() < base_risk) else "paid"
            truth_by_ref[ref] = outcome
            certified_refs.append(ref)
            for _ in range(int(rng.integers(0, 3))):
                j = int(rng.integers(n_rev))
                signal = 1 - base_risk
                rating = float(np.clip(rev_skill[j] * signal + (1 - rev_skill[j]) * rng.random()
                                       + rng.normal(0, 0.05), 0, 1))
                reviews.append(ReviewEvent(invoice_ref=ref, agent_handle=handles[a],
                                           reviewer=f"reviewer-{j:02d}", rating=rating))

    # apply resolved_fraction: only the first fraction of certifies have outcomes back
    n_res = int(len(certified_refs) * resolved_fraction)
    resolved = set(certified_refs[:n_res])
    row_by_ref = {r.invoice_ref: r for r in rows}
    for ref in resolved:
        row_by_ref[ref].outcome = truth_by_ref[ref]     # join the resolved outcome onto the row

    return SyntheticData(rows=rows, reviews=reviews, truth_by_ref=truth_by_ref,
                         handles=handles, true_quality={h: float(true_quality[i]) for i, h in enumerate(handles)},
                         tier={h: float(tier[i]) for i, h in enumerate(handles)})


def _corr(x: np.ndarray, y: np.ndarray) -> float:
    m = ~(np.isnan(x) | np.isnan(y))
    if m.sum() < 2:
        return float("nan")
    return float(np.corrcoef(x[m], y[m])[0, 1])


def validate_report(rows, reviews=(), *, truth_by_ref: dict[str, str] | None = None) -> dict:
    """corr(cgr, realized default) vs the naive corr(accept_rate, default) baseline.

    `truth_by_ref` (ALL certifies' outcomes) is used for the ground-truth default
    rate; if omitted it is derived from the rows' own joined outcomes (correct for
    fully-resolved live data).
    """
    rows = list(rows)
    results = compute_scores_from_rows(rows, reviews=reviews)
    cgr_by_handle = {r.agent_handle: r.cgr_score for r in results}
    nres_by_handle = {r.agent_handle: r.n_resolved for r in results}

    truth = truth_by_ref or {r.invoice_ref: r.outcome for r in rows
                             if r.outcome in ("paid", "default")}

    # per-agent realized default rate (ground truth) + naive accept rate
    certifies: dict[str, list[str]] = {}
    judgments: dict[str, list[str]] = {}
    for r in rows:
        if r.verifiability_tag == "judgment":
            judgments.setdefault(r.agent_handle, []).append(r.decision)
            if r.decision == "certify":
                certifies.setdefault(r.agent_handle, []).append(r.invoice_ref)

    handles = sorted(cgr_by_handle)
    cgr = np.array([cgr_by_handle[h] for h in handles])
    naive = np.array([
        (np.mean([d == "certify" for d in judgments.get(h, [])]) if judgments.get(h) else np.nan)
        for h in handles])
    pdef = np.array([
        (np.mean([truth.get(ref) == "default" for ref in certifies[h]])
         if certifies.get(h) else np.nan)
        for h in handles])

    corr_cgr = _corr(cgr, pdef)
    corr_naive = _corr(naive, pdef)
    total_resolved = sum(nres_by_handle.get(h, 0) for h in handles)
    total_certifies = sum(len(certifies.get(h, [])) for h in handles)
    return {
        "n_agents": len(handles),
        "n_resolved": total_resolved,
        "n_certifies": total_certifies,
        "coverage": (total_resolved / total_certifies) if total_certifies else 0.0,
        "corr_cgr_default": corr_cgr,
        "corr_naive_default": corr_naive,
        "beats_naive": (not np.isnan(corr_cgr)) and (abs(corr_cgr) > abs(corr_naive)),
        "meets_threshold": (not np.isnan(corr_cgr)) and (corr_cgr < -0.7),
        "per_agent": [
            {"agent_handle": h, "cgr_score": float(cgr_by_handle[h]),
             "n_resolved": int(nres_by_handle.get(h, 0)),
             "realized_default_rate": (None if np.isnan(pdef[i]) else float(pdef[i]))}
            for i, h in enumerate(handles)],
    }


def format_report(rep: dict, title: str = "") -> str:
    lines = [f"── CGR-v1 validation {title}".rstrip(),
             f"   agents={rep['n_agents']}  resolved_outcomes={rep['n_resolved']}  "
             f"certifies={rep['n_certifies']}  coverage={rep['coverage']*100:.0f}%",
             f"   corr(CGR, realized default)     = {rep['corr_cgr_default']:+.3f}   (want < -0.70)",
             f"   corr(naive accept-rate, default)= {rep['corr_naive_default']:+.3f}   (baseline)",
             f"   meets_threshold={rep['meets_threshold']}   beats_naive={rep['beats_naive']}"]
    return "\n".join(lines)


def _run_synthetic() -> int:
    # LIVE path (tier=None — TierGate unwired today): pure calibration on realized
    # outcomes. This is the config production runs.
    dlive = synthetic_substrate(with_tier=False)
    live = validate_report(dlive.rows, dlive.reviews, truth_by_ref=dlive.truth_by_ref)
    print(format_report(live, "— tier=None (live path)"))
    print()
    # FUTURE path (tier wired): with the evidence-gated ceiling, each agent here has
    # far more than N_LIFT resolved outcomes, so the ceiling lifts to 1.0 and
    # calibration dominates — the correlation is strongly negative again (the
    # tier+0.02 hard clamp that used to invert it is gone).
    dfut = synthetic_substrate(with_tier=True)
    fut = validate_report(dfut.rows, dfut.reviews, truth_by_ref=dfut.truth_by_ref)
    print(format_report(fut, "— tier prior + evidence-gated ceiling (future path)"))
    print("   note: agents have ≫ N_LIFT resolved outcomes → ceiling lifts, calibration dominates.")
    print()
    ok = (live["meets_threshold"] and live["beats_naive"]
          and fut["meets_threshold"] and fut["beats_naive"])
    print(f"SYNTHETIC VALIDATION: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _run_live(tenant_id: str) -> int:
    import os
    from aml.cloud.decision_trail import DecisionTrailService
    from aml.server.stores import StoreManager
    from aml.backends.postgres_gmp import PostgresGMPBackend
    db = os.environ["GRAFOMEM_DB_URL"]
    dt = DecisionTrailService(db)
    sm = StoreManager(lambda: PostgresGMPBackend(db))
    from aml.cgr.substrate import load_substrate
    rows = load_substrate(dt, sm, tenant_id)
    rep = validate_report(rows)
    print(format_report(rep, f"— tenant {tenant_id[:12]} (live)"))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="CGR-v1 validation report")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--synthetic", action="store_true", help="run on the synthetic fixture (default)")
    g.add_argument("--tenant", type=str, help="run on a live tenant's substrate (needs GRAFOMEM_DB_URL)")
    args = ap.parse_args(argv)
    if args.tenant:
        return _run_live(args.tenant)
    return _run_synthetic()


if __name__ == "__main__":
    raise SystemExit(main())
