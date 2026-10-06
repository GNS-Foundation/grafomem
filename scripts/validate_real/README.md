# `validate_real` — CGR validation on REAL outcomes

Two **reference / demo** harnesses that move the CGR-v1 claim off simulation and onto
**real, public outcomes**. They are the real-data companions to the in-package synthetic
harness [`src/aml/cgr/validate.py`](../../src/aml/cgr/validate.py) (corr ≈ −0.99 on simulated data).

> **These are on-demand references, not part of the runtime or the default CI job.**
> They port CGR-v1 (neutral Beta prior + verifiable calibration + reviewer Brier bridge)
> read-only — **no `src/aml/cgr/` scoring code is imported or changed.** Both are
> deterministic (seed 7).

## What's REAL vs constructed (read this first)

| Harness | REAL | Constructed |
|---|---|---|
| `cgr_real_validation.py` | credit applicants' features **and** their realized default outcomes (openml `default-of-credit-card-clients`, Taiwan, ~20k clients, ~22% default) | a fleet of 12 heterogeneous ML **certification agents** of varying quality — which *is* the deployed-agent scenario CGR exists to score |
| `cgr_reviewer_bridge_gjp.py` | ~1,900 **real human forecasters**, real geopolitical questions, real resolutions (Good Judgment Project, Harvard Dataverse `doi:10.7910/DVN/BPCDH5`). **Zero constructed agents.** | nothing — raters, items, and truth are all real |

## Headline results

**`cgr_real_validation.py`** (credit default, agents scored against realized default):
- corr(CGR, real default) ≈ **−1.000**, naive accept-rate baseline ≈ −0.886
- **early@25% resolved ≈ −0.996** ← *the load-bearing, non-tautological number*
- reviewer bridge: calibrated reviewer weight ≈ 0.99, adversarial ≈ 0.00
- writes `cgr_real_result.json` + `cgr_real_validation.png`

**`cgr_reviewer_bridge_gjp.py`** (reviewer bridge on real human judgment, out-of-sample over questions):
- corr(recovered weight, held-out reliability) ≈ **0.65** (temporal split; ≈ 0.67 random-split)
- calibration-weighted crowd **≈ −15%** Brier vs naive equal-weight (≈ −13.8% random-split)
- top-quartile raters Brier ≈ 0.094 vs bottom-quartile ≈ 0.228; weight range 0.98 / 0.00
- writes `cgr_reviewer_bridge_result.json` + `cgr_reviewer_bridge.png`

## Honest caveats (so the numbers can't be over-claimed from the repo alone)

- **The −1.000 is near-definitional.** At full resolution the Beta mean converges to the
  realized rate by construction. **Lead with `early@25%` and the reviewer bridge**, which are
  out-of-sample and therefore load-bearing.
- The GJP bridge is the purest proof: calibration is learned on TRAIN questions and evaluated
  on a **disjoint TEST** set, so the recovered trust is *not* definitional.
- **This is public data — it proves the *method*, not the *moat*.** The private-data advantage
  is out of scope here by design.

## Reproduce

Requires the optional `validate` deps **and outbound network** (first run downloads public
datasets; both cache locally afterward):

```bash
pip install -e ".[validate]"
cd scripts/validate_real
python cgr_real_validation.py        # openml fetch on first run
python cgr_reviewer_bridge_gjp.py    # ~30 MB Harvard Dataverse fetch on first run, then cached
```

Outputs (`*.json`, `*.png`) and the dataset caches (`ifps.csv`, `survey_fcasts.yr1.csv`) are
written to the working directory and are git-ignored (see `.gitignore` here).

## Why these are NOT in the default CI job

Both fetch datasets over the network (openml; Harvard Dataverse), so they can't run in a
hermetic CI job. The default CI test job scopes to `tests/` (`[tool.pytest.ini_options]
testpaths = ["tests"]`), so `scripts/` is never collected — these stay on-demand. Their deps
live in the `validate` extra, which is deliberately excluded from the `all` extra CI installs.
