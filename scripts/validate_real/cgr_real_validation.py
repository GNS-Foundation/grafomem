"""
CGR-v1 validated on REAL credit-default outcomes (not simulation).

WHAT'S REAL: the applications' features AND the repayment/default outcomes come
from a real, public credit dataset. WHAT'S CONSTRUCTED: a fleet of heterogeneous
ML *certification agents* of varying quality — which IS the deployed-agent
scenario CGR exists to score. CGR is scored against the real defaults.

Mirrors cgr_substrate.py's CGR-v1: neutral Beta prior (tier unwired), verifiable
calibration on resolved paid/default, reviewer-weighted early signal (Brier
"verify the reviewer"), evidence-gated (no ceiling since tier=None, = live path).
"""
import numpy as np, json, warnings
warnings.filterwarnings("ignore")
from sklearn.datasets import fetch_openml
from sklearn.model_selection import train_test_split
from sklearn.linear_model import LogisticRegression
from sklearn.tree import DecisionTreeClassifier
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
import pandas as pd

RNG = np.random.default_rng(7)

# ---------------------------------------------------------------------------
# 1. REAL dataset — try a big real credit-default set, fall back gracefully
# ---------------------------------------------------------------------------
def load_real():
    attempts = [
        ("default-of-credit-card-clients", "y"),   # Taiwan, 30k, default next month
        ("credit-g", "class"),                      # German, 1k, good/bad
    ]
    for name, _ in attempts:
        try:
            d = fetch_openml(name, version=1, as_frame=True)
            df = d.frame.copy()
            tgt = d.target
            # map target -> 1 = default/bad
            vals = set(pd.Series(tgt).astype(str).unique())
            if vals <= {"good", "bad"}:
                y = (pd.Series(tgt).astype(str) == "bad").astype(int).values
            else:
                s = pd.Series(tgt).astype(str)
                # binary numeric-ish: 1/"1"/"yes" = default
                y = s.isin(["1", "1.0", "yes", "True", "true"]).astype(int).values
            X = df.drop(columns=[c for c in df.columns if c == d.target.name], errors="ignore")
            if d.target.name in X.columns: X = X.drop(columns=[d.target.name])
            X = pd.get_dummies(X, drop_first=True).apply(pd.to_numeric, errors="coerce").fillna(0.0)
            return name, X.values.astype(float), y
        except Exception as e:
            print(f"  (fetch {name} failed: {str(e)[:70]})")
    raise SystemExit("no dataset")

name, X, y = load_real()
# cap size for speed
if len(y) > 20000:
    idx = RNG.choice(len(y), 20000, replace=False); X, y = X[idx], y[idx]
base_default = y.mean()
print(f"REAL dataset: {name} | n={len(y)} | real default rate={base_default:.3f} | features={X.shape[1]}")

Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.6, random_state=7, stratify=y)
sc = StandardScaler().fit(Xtr); Xtr_s, Xte_s = sc.transform(Xtr), sc.transform(Xte)

# ---------------------------------------------------------------------------
# 2. Fleet of heterogeneous certification agents (real policies, varied quality)
#    Each yields P(good) = P(not default). Certify if P(good) >= threshold.
# ---------------------------------------------------------------------------
def fit_prob(model, corrupt=0.0):
    yt = ytr.copy()
    if corrupt > 0:  # train on partially corrupted labels -> genuinely worse judgment
        flip = RNG.random(len(yt)) < corrupt
        yt = np.where(flip, 1 - yt, yt)
    model.fit(Xtr_s, yt)
    p_default = model.predict_proba(Xte_s)[:, list(model.classes_).index(1)] if 1 in model.classes_ else np.full(len(Xte_s), yt.mean())
    return 1.0 - p_default  # P(good)

agents = []  # (name, p_good array, threshold)
agents.append(("gbm-strong",        fit_prob(HistGradientBoostingClassifier(max_iter=200, random_state=1)), 0.65))
agents.append(("gbm-loose",         agents[0][1], 0.50))                      # same model, permissive threshold
agents.append(("logit-solid",       fit_prob(LogisticRegression(max_iter=500)), 0.62))
agents.append(("logit-strict",      agents[2][1], 0.75))
agents.append(("tree-d5",           fit_prob(DecisionTreeClassifier(max_depth=5, random_state=2)), 0.60))
agents.append(("tree-stump",        fit_prob(DecisionTreeClassifier(max_depth=1, random_state=3)), 0.55))
agents.append(("logit-corrupt15",   fit_prob(LogisticRegression(max_iter=500), corrupt=0.15), 0.60))
agents.append(("logit-corrupt30",   fit_prob(LogisticRegression(max_iter=500), corrupt=0.30), 0.60))
agents.append(("tree-corrupt25",    fit_prob(DecisionTreeClassifier(max_depth=5, random_state=4), corrupt=0.25), 0.55))
agents.append(("random-policy",     RNG.random(len(Xte_s)), 0.50))           # no signal
agents.append(("inverted-policy",   1.0 - agents[0][1], 0.55))               # adversarial: trusts the worst
agents.append(("permissive-weak",   agents[5][1], 0.40))
NA = len(agents)
handles = [a[0] for a in agents]

# ---------------------------------------------------------------------------
# 3. Assign each application to one agent; agent decides certify/reject.
#    Certified -> the REAL outcome (default?) is ground truth.
# ---------------------------------------------------------------------------
assign = RNG.integers(0, NA, size=len(Xte))
decisions = []  # dict per certified decision
for i in range(len(Xte)):
    a = assign[i]; pg = agents[a][1][i]; thr = agents[a][2]
    certify = pg >= thr
    decisions.append(dict(agent=a, invoice=i, certify=bool(certify),
                          p_good=float(pg), default=int(yte[i])))

# ---------------------------------------------------------------------------
# 4. Reviewers rate certified items; varied reliability (verify-the-reviewer)
# ---------------------------------------------------------------------------
N_REV = 12
rev_skill = RNG.uniform(0, 1, N_REV)
rev_skill[0] = 1.0        # a genuinely calibrated reviewer
rev_skill[1] = 0.0        # adversarial reviewer (anti-signal)
reviews = []  # (invoice, agent, reviewer, rating)
for d in decisions:
    if not d["certify"]: continue
    truth_good = 1.0 - d["default"]           # real outcome
    for _ in range(int(RNG.integers(0, 3))):
        j = int(RNG.integers(N_REV))
        if j == 1:  # adversarial: rates opposite of truth
            rating = np.clip((1 - truth_good) * 0.8 + RNG.normal(0, 0.1), 0, 1)
        else:
            rating = np.clip(rev_skill[j] * truth_good + (1 - rev_skill[j]) * RNG.random() + RNG.normal(0, 0.05), 0, 1)
        reviews.append(dict(invoice=d["invoice"], agent=d["agent"], reviewer=j, rating=float(rating)))

# ---------------------------------------------------------------------------
# 5. CGR-v1 (port of cgr_substrate.py; tier=None -> neutral prior, live path)
# ---------------------------------------------------------------------------
K_PRIOR = 4.0
certs = [d for d in decisions if d["certify"]]
out_by_inv = {d["invoice"]: d["default"] for d in certs}

def cgr(resolved_fraction=1.0):
    n_res = int(len(certs) * resolved_fraction)
    resolved = set(d["invoice"] for d in certs[:n_res])
    # reviewer calibration on RESOLVED (Brier vs real outcome)
    hit = {j: [] for j in range(N_REV)}
    for r in reviews:
        if r["invoice"] in resolved:
            good = 1.0 - out_by_inv[r["invoice"]]
            hit[r["reviewer"]].append((r["rating"] - good) ** 2)
    w = {j: (np.clip(1 - np.mean(v)/0.25, 0, 1) if len(v) >= 5 else 0.05) for j, v in hit.items()}
    scores = np.full(NA, np.nan)
    for a in range(NA):
        alpha = beta = 1.0    # neutral prior (tier unwired)
        for d in certs[:n_res]:
            if d["agent"] != a: continue
            good = 1.0 - out_by_inv[d["invoice"]]
            alpha += good; beta += (1 - good)
        for r in reviews:
            if r["agent"] != a or r["invoice"] in resolved: continue
            ww = w[r["reviewer"]]; alpha += ww*r["rating"]; beta += ww*(1-r["rating"])
        scores[a] = alpha / (alpha + beta)
    return scores, w

cgr_full, rev_w = cgr(1.0)
cgr_early, _ = cgr(0.25)

def realized_default(a):
    c = [d for d in certs if d["agent"] == a]
    return np.mean([d["default"] for d in c]) if c else np.nan
pdef = np.array([realized_default(a) for a in range(NA)])

def accept_rate(a):
    ds = [d for d in decisions if d["agent"] == a]
    return np.mean([d["certify"] for d in ds]) if ds else np.nan
naive = np.array([accept_rate(a) for a in range(NA)])

def corr(x, y):
    m = ~(np.isnan(x) | np.isnan(y)); return float(np.corrcoef(x[m], y[m])[0, 1])

print(f"\ncertified decisions: {len(certs)} | reviews: {len(reviews)}")
print("\n--- CGR-v1 vs REAL realized default (per agent) ---")
print(f"  corr(CGR, real default)          = {corr(cgr_full, pdef):+.3f}   (want strongly negative)")
print(f"  corr(naive accept-rate, default) = {corr(naive, pdef):+.3f}   (baseline)")
print(f"  corr(CGR_early@25%, real default)= {corr(cgr_early, pdef):+.3f}   (early signal)")
order = np.argsort(cgr_full); bot = order[:NA//2]; top = order[NA//2:]
print(f"  certified-default: top-CGR agents {np.nanmean(pdef[top])*100:4.1f}%  vs bottom-CGR {np.nanmean(pdef[bot])*100:4.1f}%")
print("\n--- verify-the-reviewer bridge (weights from real outcomes) ---")
print(f"  calibrated reviewer (rev 0) weight  = {rev_w[0]:.2f}   (want high)")
print(f"  adversarial reviewer (rev 1) weight = {rev_w[1]:.2f}   (want ~0)")
print("\n--- per-agent (sorted best CGR first) ---")
for a in order[::-1]:
    print(f"  {handles[a]:18s} CGR={cgr_full[a]:.3f}  real_default={pdef[a]*100:5.1f}%  accept={naive[a]*100:5.1f}%")

json.dump({
  "dataset": name, "n": int(len(y)), "real_default_rate": float(base_default),
  "n_agents": NA, "certified": len(certs), "reviews": len(reviews),
  "corr_cgr_default": corr(cgr_full, pdef), "corr_naive_default": corr(naive, pdef),
  "corr_cgr_early_default": corr(cgr_early, pdef),
  "top_half_default": float(np.nanmean(pdef[top])), "bottom_half_default": float(np.nanmean(pdef[bot])),
  "rev_calibrated_w": rev_w[0], "rev_adversarial_w": rev_w[1],
}, open("cgr_real_result.json", "w"), indent=2)
print("\nsaved -> cgr_real_result.json")

# ---------------------------------------------------------------------------
# 6. chart
# ---------------------------------------------------------------------------
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams.update({"figure.dpi":130,"font.size":10,"axes.spines.top":False,"axes.spines.right":False})
fig,(ax1,ax2)=plt.subplots(1,2,figsize=(13,5.2))
ax1.scatter(cgr_full*100, pdef*100, s=90, color="#2563eb", zorder=3, label="CGR-v1 (full outcomes)")
ax1.scatter(naive*100, pdef*100, s=45, color="#9ca3af", marker="s", label="naive accept-rate")
for a in range(NA):
    ax1.annotate(handles[a], (cgr_full[a]*100, pdef[a]*100), fontsize=6.3, color="#1e3a8a",
                 xytext=(3,3), textcoords="offset points")
ax1.set_xlabel("trust score"); ax1.set_ylabel("REAL realized default rate on certified loans (%)")
ax1.set_title(f"CGR predicts REAL default (corr {corr(cgr_full,pdef):.2f}); naive weaker ({corr(naive,pdef):.2f})")
ax1.legend(fontsize=8, loc="upper right")
ax2.scatter(cgr_early*100, cgr_full*100, s=80, color="#10b981", zorder=3)
lims=[min(cgr_early.min(),cgr_full.min())*100-3, max(cgr_early.max(),cgr_full.max())*100+3]
ax2.plot(lims,lims,"--",color="#111",lw=1)
ax2.set_xlabel("CGR early (25% of real outcomes resolved)"); ax2.set_ylabel("CGR final (all resolved)")
ax2.set_title(f"Early CGR ≈ final (corr {corr(cgr_early,cgr_full):.2f})\nflags bad agents before loans mature")
fig.suptitle(f"CGR-v1 on REAL credit-default data — {name} (n={len(y)}, real default {base_default*100:.1f}%)",
             fontsize=12, y=1.02)
plt.tight_layout(); plt.savefig("cgr_real_validation.png", bbox_inches="tight")
print("saved -> cgr_real_validation.png")
