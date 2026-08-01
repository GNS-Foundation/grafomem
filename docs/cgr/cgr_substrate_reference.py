"""
CGR-v1 substrate — reference implementation on Kapwork-shaped receivables data.

Purpose: prove the "capture now, score later" instrumentation for the Kapwork
GRAFOMEM POC. It (1) defines the exact events to log from day one, (2) generates
synthetic invoice certifications + delayed paid/default outcomes + funder reviews,
(3) joins them, and (4) computes CGR-v1 for each certification agent — showing the
score predicts which agents certify invoices that later DEFAULT, beats a naive
baseline, and gives an EARLY signal before outcomes resolve.

The three irreversible capture fields (lose them on invoice #1 and the moat is
gone): stable INVOICE_REF (join key), stable AGENT_HANDLE (attribution), and the
VERIFIABILITY_TAG (rule vs judgment — separates the calibration slice from the
value slice).

Run: python3 cgr_substrate.py
"""
import numpy as np, matplotlib.pyplot as plt, matplotlib as mpl, json, os
os.makedirs("out", exist_ok=True)
RNG = np.random.default_rng(7)

# ---------------------------------------------------------------------------
# 0. THE CAPTURE SCHEMA (what GRAFOMEM logs from day one)
# ---------------------------------------------------------------------------
# DecisionEvent  (at decision time; extends /v1/governed/decisions payload)
#   decision_id, agent_handle (GEIANT), agent_tier, model_id,
#   invoice_ref  <-- STABLE JOIN KEY, amount, po_amount, approval_present,
#   duplicate, decision(certify|reject), reason_code, reason_text,
#   verifiability_tag(rule|judgment)  <-- separates calibration vs value slice,
#   agent_confidence, ts
# OutcomeEvent   (arrives LATER, async; stored as a GMP Fact)
#   invoice_ref (join), outcome(paid|default), outcome_date, days_to_outcome
# ReviewEvent    (optional; funder/analyst rating; stored as a GMP Fact)
#   decision_id, reviewer_handle, rating in [0,1], ts

# ---------------------------------------------------------------------------
# 1. Ground truth (hidden): agents with a true judgment quality + capability tier
# ---------------------------------------------------------------------------
N_AGENTS = 12
true_quality = RNG.uniform(0.35, 0.95, N_AGENTS)          # skill at the JUDGMENT call
# GEIANT TierGate capability tier (noisy proxy of quality) -> the CGR prior
tier = np.clip(true_quality + RNG.normal(0, 0.08, N_AGENTS), 0, 1)
agent_handles = [f"invoice-certifier-{i:02d}@kapwork-receivables" for i in range(N_AGENTS)]

N_INV = 4000
decisions, outcomes, reviews = [], [], []

# funder reviewers with per-reviewer reliability (for the verify-the-reviewer slice)
N_REV = 20
rev_skill = RNG.uniform(0.0, 1.0, N_REV)

for k in range(N_INV):
    a = RNG.integers(N_AGENTS)
    amount = float(RNG.uniform(1_000, 200_000))
    po_amount = amount * (1 + RNG.normal(0, 0.05)) if RNG.random() < 0.9 else amount * 1.5
    approval_present = RNG.random() < 0.85
    duplicate = RNG.random() < 0.04
    # latent default risk of this invoice (what judgment must catch)
    base_risk = RNG.beta(2.0, 4.0)                         # mean ~0.33, real mass in risky zone
    # --- rule layer (VERIFIABLE): hard rejects ---
    rule_reject = (amount > po_amount * 1.05) or (not approval_present) or duplicate
    if rule_reject:
        reason = "amount>PO" if amount > po_amount*1.05 else ("no_debtor_approval" if not approval_present else "duplicate")
        decisions.append(dict(invoice_ref=f"INV{k:05d}", agent_handle=agent_handles[a], agent=a,
                              agent_tier=tier[a], amount=amount, decision="reject",
                              reason_code=reason, verifiability_tag="rule", risk=base_risk,
                              confidence=0.99))
        continue
    # --- judgment layer (UNVERIFIABLE): certify unless the agent's judgment flags risk ---
    # good agents' perceived risk tracks true risk; poor agents are noisy (wide spread)
    perceived = np.clip(base_risk + RNG.normal(0, 0.45*(1-true_quality[a])+0.03), 0, 1)
    certify = perceived < 0.50                             # reject on judgment if risk looks high
    dec = "certify" if certify else "reject"
    decisions.append(dict(invoice_ref=f"INV{k:05d}", agent_handle=agent_handles[a], agent=a,
                          agent_tier=tier[a], amount=amount, decision=dec,
                          reason_code="risk_judgment" if not certify else "clean",
                          verifiability_tag="judgment", risk=base_risk,
                          confidence=float(1-perceived)))
    if certify:
        # OUTCOME arrives later: certified risky invoices default more often
        default = RNG.random() < base_risk
        outcomes.append(dict(invoice_ref=f"INV{k:05d}", outcome="default" if default else "paid",
                             days_to_outcome=int(RNG.uniform(20, 120))))
        # a couple of funder reviews on this certification (rating ~ tracks truth by reviewer skill)
        for _ in range(RNG.integers(0, 3)):
            j = RNG.integers(N_REV)
            signal = (1-base_risk)                          # truly-good invoice -> high rating
            rating = np.clip(rev_skill[j]*signal + (1-rev_skill[j])*RNG.random() + RNG.normal(0,0.05),0,1)
            reviews.append(dict(invoice_ref=f"INV{k:05d}", agent=a, reviewer=j, rating=float(rating)))

print(f"Logged: {len(decisions)} decisions | {len(outcomes)} outcomes (certified) | {len(reviews)} reviews")
rule_share = np.mean([d['verifiability_tag']=='rule' for d in decisions])
print(f"Rule (verifiable) rejects: {rule_share*100:.0f}%  |  judgment calls: {(1-rule_share)*100:.0f}%")

# ---------------------------------------------------------------------------
# 2. JOIN + CGR-v1 per agent
# ---------------------------------------------------------------------------
out_by_inv = {o['invoice_ref']: o for o in outcomes}
K_PRIOR = 4.0

def cgr_scores(resolved_fraction=1.0):
    """Compute CGR per agent using capability prior + resolved-outcome calibration.
    resolved_fraction<1 simulates scoring EARLY (only some outcomes back yet)."""
    # reviewer calibration on RESOLVED invoices (verify the reviewer, not the task)
    rev_hit = {j: [] for j in range(N_REV)}
    resolved_refs = set()
    certified = [d for d in decisions if d['decision']=="certify"]
    n_res = int(len(certified)*resolved_fraction)
    for d in certified[:n_res]:
        resolved_refs.add(d['invoice_ref'])
    for r in reviews:
        if r['invoice_ref'] in resolved_refs:
            o = out_by_inv[r['invoice_ref']]; good = 1.0 if o['outcome']=="paid" else 0.0
            rev_hit[r['reviewer']].append((r['rating']-good)**2)
    rev_w = {j: (np.clip(1-np.mean(v)/0.25,0,1) if len(v)>=5 else 0.05) for j,v in rev_hit.items()}

    scores=np.full(N_AGENTS, np.nan); ndata=np.zeros(N_AGENTS)
    for a in range(N_AGENTS):
        # capability prior from GEIANT tier
        alpha = 1 + K_PRIOR*tier[a]; beta = 1 + K_PRIOR*(1-tier[a])
        for d in certified[:n_res]:
            if d['agent']!=a: continue
            o = out_by_inv[d['invoice_ref']]; good = 1.0 if o['outcome']=="paid" else 0.0
            alpha += good; beta += (1-good); ndata[a]+=1
        # reviewer-weighted signal on UNRESOLVED certifications (early trust)
        for r in reviews:
            if r['agent']!=a or r['invoice_ref'] in resolved_refs: continue
            w = rev_w[r['reviewer']]; alpha += w*r['rating']; beta += w*(1-r['rating'])
        scores[a] = alpha/(alpha+beta)
    return scores, ndata

cgr, ndata = cgr_scores(1.0)
cgr_early, _ = cgr_scores(0.25)      # only 25% of outcomes resolved yet

# realized default rate on each agent's certified portfolio (ground-truth quality)
def portfolio_default(a):
    certs=[d for d in decisions if d['agent']==a and d['decision']=="certify"]
    if not certs: return np.nan
    return np.mean([out_by_inv[d['invoice_ref']]['outcome']=="default" for d in certs])
pdef = np.array([portfolio_default(a) for a in range(N_AGENTS)])

# naive baseline: trust = certification acceptance rate (volume-based, no outcomes)
def accept_rate(a):
    ds=[d for d in decisions if d['agent']==a and d['verifiability_tag']=="judgment"]
    return np.mean([d['decision']=="certify" for d in ds]) if ds else np.nan
naive = np.array([accept_rate(a) for a in range(N_AGENTS)])

def corr(x,y):
    m=~(np.isnan(x)|np.isnan(y)); return float(np.corrcoef(x[m],y[m])[0,1])
print("\n--- CGR-v1 vs realized default (lower default = better agent) ---")
print(f"  corr(CGR, portfolio default)        = {corr(cgr,pdef):+.2f}   (want strongly negative)")
print(f"  corr(naive accept-rate, default)    = {corr(naive,pdef):+.2f}   (baseline)")
print(f"  corr(CGR_early@25% outcomes, default)= {corr(cgr_early,pdef):+.2f}  (early signal, pre-resolution)")
print(f"  corr(CGR, true judgment quality)    = {corr(cgr,true_quality):+.2f}")
print(f"  corr(portfolio default, true quality)= {corr(pdef,true_quality):+.2f}  (chain check)")
# decision value: default rate of top-half vs bottom-half CGR agents
order=np.argsort(cgr); bot=order[:N_AGENTS//2]; top=order[N_AGENTS//2:]
print(f"\n  certified-default rate: top-CGR agents {np.nanmean(pdef[top])*100:.1f}%  "
      f"vs bottom-CGR {np.nanmean(pdef[bot])*100:.1f}%")

# ---------------------------------------------------------------------------
# 3. chart
# ---------------------------------------------------------------------------
mpl.rcParams.update({"figure.dpi":130,"font.size":10,"axes.spines.top":False,"axes.spines.right":False})
fig,(ax1,ax2)=plt.subplots(1,2,figsize=(13,5))
ax1.scatter(cgr*100, pdef*100, s=90, color="#2563eb", zorder=3, label="CGR-v1 (full outcomes)")
ax1.scatter(naive*100, pdef*100, s=55, color="#9ca3af", marker="s", label="naive accept-rate")
for a in range(N_AGENTS):
    ax1.annotate(str(a), (cgr[a]*100, pdef[a]*100), fontsize=7, color="#1e3a8a")
ax1.set_xlabel("trust score"); ax1.set_ylabel("realized default rate on certified portfolio (%)")
ax1.set_title("CGR predicts default (steep ↓); naive doesn't")
ax1.legend(fontsize=8)
# early vs late
ax2.scatter(cgr_early*100, cgr*100, s=80, color="#10b981", zorder=3)
lims=[min(cgr_early.min(),cgr.min())*100-2, max(cgr_early.max(),cgr.max())*100+2]
ax2.plot(lims,lims,"--",color="#111",lw=1)
ax2.set_xlabel("CGR early (25% of outcomes resolved)"); ax2.set_ylabel("CGR final (all resolved)")
ax2.set_title(f"Early CGR ≈ final CGR (corr {corr(cgr_early,cgr):.2f})\nusable trust signal before invoices resolve")
plt.tight_layout(); plt.savefig("out/cgr_substrate.png", bbox_inches="tight")
print("\nSaved -> out/cgr_substrate.png")

# emit sample logged records (the substrate, as it would land in GRAFOMEM)
open("out/cgr_substrate_sample.json","w").write(json.dumps({
    "decision_event_example": decisions[0],
    "outcome_event_example": outcomes[0] if outcomes else None,
    "review_event_example": reviews[0] if reviews else None,
    "counts": {"decisions":len(decisions),"outcomes":len(outcomes),"reviews":len(reviews)},
}, indent=2, default=float))
print("Saved -> out/cgr_substrate_sample.json")
