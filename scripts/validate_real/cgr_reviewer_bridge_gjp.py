"""
CGR reviewer-bridge — validated on REAL multi-rater human judgment (Good Judgment Project).

Purest real-data proof of the "verify the reviewer, not the task" mechanism:
  - RATERS  = real individual forecasters (thousands of humans), ZERO constructed agents.
  - ITEMS   = real geopolitical questions (IFPs).
  - TRUTH   = real resolved outcomes.

The load-bearing design choice: we calibrate each rater on a set of resolved questions
(TRAIN) and evaluate the recovered trust on a DISJOINT set of questions (TEST). Because the
evaluation is out-of-sample over questions, the result is NOT definitional — unlike a
full-resolution Beta mean, which converges to the realized rate by construction.

Source: Good Judgment Project, Harvard Dataverse doi:10.7910/DVN/BPCDH5 (IARPA ACE, open reuse).
Deterministic (seed 7).
"""
import io, json, urllib.request
import numpy as np
import pandas as pd

SEED = 7
rng = np.random.default_rng(SEED)
BRIER_UNINFORMED = 0.25          # Brier of the always-0.5 coin-flip reviewer
MIN_TRAIN = 15                   # min resolved TRAIN questions to estimate a rater's calibration
MIN_TEST = 8                     # min resolved TEST questions to score held-out reliability
MIN_RATERS_PER_Q = 15           # min raters on a test question to form a meaningful crowd

DV = "https://dataverse.harvard.edu/api/access/datafile/:persistentId/?persistentId={pid}&format=original"

def _load(pid, fname):
    import os
    if os.path.exists(fname):
        return open(fname, "rb").read()
    raw = urllib.request.urlopen(
        urllib.request.Request(DV.format(pid=pid), headers={"User-Agent": "python-dataset-loader"}),
        timeout=180).read()
    open(fname, "wb").write(raw)
    return raw

def brier_single(p):  # Brier on P(correct outcome): mean((1-p)^2)
    p = np.asarray(p, float)
    return np.mean((1.0 - p) ** 2)

def weight_from_brier(b):  # calibrated reviewer earns weight; coin-flip earns ~0
    return float(np.clip(1.0 - b / BRIER_UNINFORMED, 0.0, 1.0))

def main():
    # ---- questions with real resolutions (binary slice for a clean Brier) ----
    ifps = pd.read_csv(io.BytesIO(_load("doi:10.7910/DVN/BPCDH5/L8WZEF", "ifps.csv")), encoding="latin-1")
    ifps["outcome"] = ifps["outcome"].astype(str).str.strip()
    binq = ifps[(ifps["n_opts"] == 2) & (ifps["outcome"].isin(["a", "b"]))].copy()
    binq["date_start"] = pd.to_datetime(binq["date_start"], errors="coerce")
    binq = binq.dropna(subset=["date_start"])
    outcome = dict(zip(binq["ifp_id"], binq["outcome"]))
    qdate = dict(zip(binq["ifp_id"], binq["date_start"]))
    print(f"resolved binary questions: {len(binq)}")

    # ---- individual forecasts (real humans) ----
    fc = pd.read_csv(io.BytesIO(_load("doi:10.7910/DVN/BPCDH5/VHXOB2", "survey_fcasts.yr1.csv")), encoding="latin-1")
    fc = fc[fc["ifp_id"].isin(outcome)].copy()
    fc["timestamp"] = pd.to_datetime(fc["timestamp"], errors="coerce")
    # keep only the probability the forecaster placed on the option that actually occurred
    fc["is_correct_opt"] = [ao == outcome[i] for ao, i in zip(fc["answer_option"], fc["ifp_id"])]
    fc = fc[fc["is_correct_opt"]].copy()
    fc = fc.dropna(subset=["timestamp", "value"])
    fc["value"] = fc["value"].clip(0, 1)
    # each rater's FINAL forecast on each question (latest timestamp)
    fc = fc.sort_values("timestamp").groupby(["ifp_id", "user_id"], as_index=False).last()
    print(f"final (rater,question) forecasts: {len(fc)}  raters={fc.user_id.nunique()}  questions={fc.ifp_id.nunique()}")

    def run_split(train_q, test_q, tag):
        tr = fc[fc.ifp_id.isin(train_q)]
        te = fc[fc.ifp_id.isin(test_q)]
        # per-rater calibration on TRAIN
        rec = []
        for uid, g in tr.groupby("user_id"):
            if len(g) >= MIN_TRAIN:
                rec.append((uid, len(g), brier_single(g["value"].values)))
        cal = pd.DataFrame(rec, columns=["user_id", "n_train", "brier_train"])
        cal["weight"] = cal["brier_train"].map(weight_from_brier)
        # held-out individual skill on TEST (same skill scale as weight)
        rows = []
        for uid, g in te.groupby("user_id"):
            if len(g) >= MIN_TEST:
                rows.append((uid, len(g), brier_single(g["value"].values)))
        sk = pd.DataFrame(rows, columns=["user_id", "n_test", "brier_test"])
        sk["skill_test"] = sk["brier_test"].map(weight_from_brier)
        m = cal.merge(sk, on="user_id")   # raters present in BOTH train & test
        corr = float(np.corrcoef(m["weight"], m["skill_test"])[0, 1])

        # crowd aggregation on held-out TEST questions, over the calibrated rater pool
        wmap = dict(zip(m["user_id"], m["weight"]))
        pool = te[te.user_id.isin(wmap)]
        naive_b, cal_b = [], []
        for qid, g in pool.groupby("ifp_id"):
            if g.user_id.nunique() < MIN_RATERS_PER_Q:
                continue
            p = g["value"].values
            w = np.array([wmap[u] for u in g["user_id"].values])
            naive_b.append((1.0 - p.mean()) ** 2)
            if w.sum() > 0:
                cal_b.append((1.0 - np.average(p, weights=w)) ** 2)
            else:
                cal_b.append((1.0 - p.mean()) ** 2)
        naive_crowd = float(np.mean(naive_b)); cal_crowd = float(np.mean(cal_b))
        # decision value: held-out reliability of top- vs bottom-quartile-by-weight raters
        qhi, qlo = m["weight"].quantile(0.75), m["weight"].quantile(0.25)
        top_b = float(m[m.weight >= qhi]["brier_test"].mean())
        bot_b = float(m[m.weight <= qlo]["brier_test"].mean())
        res = dict(tag=tag, n_raters_matched=int(len(m)), n_train_q=int(len(train_q)),
                   n_test_q=int(len(test_q)), n_test_q_scored=int(len(cal_b)),
                   corr_weight_vs_heldout_skill=round(corr, 3),
                   brier_naive_crowd=round(naive_crowd, 4), brier_calibrated_crowd=round(cal_crowd, 4),
                   crowd_improvement_pct=round(100 * (naive_crowd - cal_crowd) / naive_crowd, 1),
                   brier_test_top_quartile=round(top_b, 4), brier_test_bottom_quartile=round(bot_b, 4),
                   weight_max=round(float(m.weight.max()), 2), weight_min=round(float(m.weight.min()), 2))
        return res, m

    qids = sorted(fc.ifp_id.unique())          # only questions actually forecast in this file
    qids_dated = sorted(qids, key=lambda q: qdate[q])
    # diagnostics: how many resolved questions each rater answered
    per_rater = fc.groupby("user_id").size()
    print(f"raters with >=20 answered: {(per_rater>=20).sum()}  >=30: {(per_rater>=30).sum()}  "
          f"median answered: {per_rater.median():.0f}")
    # PRIMARY: temporal split — calibrate on earlier questions, evaluate on later ones
    cut = int(0.6 * len(qids_dated))
    temporal, m_temporal = run_split(set(qids_dated[:cut]), set(qids_dated[cut:]), "temporal")
    # ROBUSTNESS: random split
    perm = rng.permutation(qids)
    rcut = int(0.6 * len(perm))
    random_s, _ = run_split(set(perm[:rcut]), set(perm[rcut:]), "random")

    out = {"dataset": "Good Judgment Project yr1 (Harvard Dataverse doi:10.7910/DVN/BPCDH5)",
           "n_resolved_binary_questions": int(len(binq)),
           "n_final_forecasts": int(len(fc)),
           "n_unique_raters": int(fc.user_id.nunique()),
           "temporal_split": temporal, "random_split": random_s}
    json.dump(out, open("cgr_reviewer_bridge_result.json", "w"), indent=2)
    print(json.dumps(out, indent=2))
    _plot(m_temporal, temporal)
    return out, m_temporal


def _plot(m, res):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.5, 5.4))
    fig.suptitle("CGR reviewer-bridge on REAL human judgment — Good Judgment Project (yr1, "
                 f"{res['n_raters_matched']:,} raters, out-of-sample over questions)",
                 fontsize=12.5, fontweight="bold")

    # Panel A: recovered trust (train) predicts held-out reliability (test)
    x, y = m["weight"].values, m["skill_test"].values
    ax1.scatter(x, y, s=10, alpha=0.30, color="#1f6feb", edgecolors="none")
    b, a = np.polyfit(x, y, 1)
    xs = np.linspace(0, 1, 50)
    ax1.plot(xs, a + b * xs, color="#d1242f", lw=2, label=f"fit  (r = {res['corr_weight_vs_heldout_skill']})")
    ax1.set_xlabel("Recovered reviewer weight  (calibrated on TRAIN questions)")
    ax1.set_ylabel("Held-out reliability  (measured on TEST questions)")
    ax1.set_title("A.  'Verify the reviewer' recovers who to trust —\nout-of-sample, no constructed agents", fontsize=10.5)
    ax1.legend(loc="upper left", fontsize=9); ax1.grid(alpha=0.25)
    ax1.set_xlim(-0.02, 1.02); ax1.set_ylim(-0.02, 1.02)

    # Panel B: the decision value in Brier points (lower = better)
    labels = ["Naive\nequal-weight\ncrowd", "Calibration-\nweighted\ncrowd",
              "Top-quartile\nraters", "Bottom-quartile\nraters"]
    vals = [res["brier_naive_crowd"], res["brier_calibrated_crowd"],
            res["brier_test_top_quartile"], res["brier_test_bottom_quartile"]]
    colors = ["#8b949e", "#1a7f37", "#1a7f37", "#d1242f"]
    bars = ax2.bar(labels, vals, color=colors, width=0.66)
    for bar, v in zip(bars, vals):
        ax2.text(bar.get_x() + bar.get_width()/2, v + 0.004, f"{v:.3f}", ha="center", fontsize=9.5)
    ax2.set_ylabel("Held-out Brier score  (lower = better judgment)")
    ax2.set_title(f"B.  The decision value on held-out questions\ncalibrated crowd −{res['crowd_improvement_pct']}% Brier vs naive",
                  fontsize=10.5)
    ax2.grid(alpha=0.25, axis="y"); ax2.set_ylim(0, max(vals) * 1.18)
    fig.tight_layout(rect=[0, 0, 1, 0.94])
    fig.savefig("cgr_reviewer_bridge.png", dpi=140)
    print("wrote cgr_reviewer_bridge.png")

if __name__ == "__main__":
    main()
