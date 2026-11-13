# Claude Code Ticket — `validate_real` reference harnesses (grafomem)

**Repo:** `~/grafomem`  ·  **Owner (architect):** Camilo + Cowork-chat (spec)  ·  **You:** implementer
**Base:** branch `cgr/validate-real` off `main`.
**Scope:** Commit the two **real-data** CGR validation harnesses as a `validate_real` reference so Claude Code, CI, and CDP-facing demos can re-run the real-outcome proofs on demand. **Reference / demo code only — no change to `src/aml/cgr/` scoring, no new runtime dependency on these at import time.**

> **Context (why):** We already have `src/aml/cgr/validate.py` (synthetic/simulation, corr ≈ −0.99). These two new harnesses move the claim onto **real outcomes** and belong in the repo next to it so nobody has to reconstruct them: (1) credit-default agent-scoring proof, (2) the reviewer-bridge proof on real human judgment. Both are deterministic (seed 7) and self-contained.

## The two files to add
Place under `scripts/validate_real/` (or `src/aml/cgr/validate_real/` if you'd rather keep them import-isolated alongside `validate.py` — your call, but if inside the package they MUST stay out of the import graph: no top-level import pulls sklearn/pandas/urllib; guard everything under `if __name__ == "__main__"` or a lazy function). The architect leans **`scripts/validate_real/`** to keep the package's isolation grep clean and avoid a heavy dep in the import path.

1. **`cgr_real_validation.py`** — credit-default agent-scoring proof.
   - Real: features + default outcomes (openml `default-of-credit-card-clients`, Taiwan, 20k clients, 22.1% default). Constructed (appropriately): a fleet of 12 heterogeneous ML certification agents.
   - Ports CGR-v1 (neutral Beta prior + verifiable calibration + reviewer Brier bridge), scores agents against **realized default**.
   - Results: corr(CGR, real default) −1.000; naive baseline −0.886; **early@25% −0.996** (the load-bearing, non-tautological one); reviewer weight 0.99 / adversarial 0.00.
   - Writes `cgr_real_result.json` + `cgr_real_validation.png`.

2. **`cgr_reviewer_bridge_gjp.py`** — reviewer-bridge proof on **real human judgment**, ZERO constructed agents.
   - Real: ~1,900 Good Judgment Project forecasters, real geopolitical questions, real resolutions (Harvard Dataverse `doi:10.7910/DVN/BPCDH5`, IARPA/open reuse).
   - Calibrates each rater on TRAIN questions → recovers reliability on **disjoint TEST** questions (out-of-sample, so not definitional).
   - Results (temporal split): corr(recovered weight, held-out reliability) **0.65**; calibration-weighted crowd **−15.0%** Brier vs naive; top-quartile 0.094 vs bottom-quartile 0.228; weight range 0.98 / 0.00. Random-split robustness consistent (0.67, −13.8%).
   - Writes `cgr_reviewer_bridge_result.json` + `cgr_reviewer_bridge.png`.

*(Both files are attached in the Cowork chat — copy them in verbatim; they run as-is with `pandas`, `numpy`, `scikit-learn`, `matplotlib`.)*

## Tasks
- **A. Add the two files** under `scripts/validate_real/` with a short `README.md` summarizing what each proves, the headline numbers above, the honest caveats (−1.000 near-definitional → lead with early@25% and the reviewer bridge; public data proves the *method*, not the *moat*), and the exact reproduce commands.
- **B. Dependencies:** add `scikit-learn` + `matplotlib` to a **dev/optional** extra (e.g. `[project.optional-dependencies].validate`), NOT core runtime. `pandas`/`numpy` are presumably already present — confirm.
- **C. Network note in the README:** `cgr_reviewer_bridge_gjp.py` fetches ~30 MB from Harvard Dataverse on first run and caches locally; `cgr_real_validation.py` fetches from openml. Document that these need outbound network and are therefore **not** wired into the default CI test job (they're on-demand references). If you want a CI smoke test, gate it behind a marker/flag that's off by default.
- **D. Import-isolation guard:** if you place anything under `src/aml/cgr/`, re-run the §isolation grep and confirm it stays clean. If under `scripts/`, note it's outside the package and isolation is N/A.
- **E. Do NOT** touch `scoring.py`, `engine.py`, `validate.py`, or any runtime path. This is additive reference material.

## Acceptance / definition of done
1. Both harnesses live in the repo, run to completion from a clean checkout (with the optional dev deps installed + network), and reproduce the headline numbers deterministically.
2. A `README.md` states clearly what is REAL vs constructed in each, the load-bearing results, and the caveats — so the numbers can never be over-claimed from the repo alone.
3. Package import isolation unaffected; core runtime deps unchanged; existing suite green.

## Non-goals
- No CI wiring of the network-fetching harnesses into the default job (on-demand only).
- No change to CGR scoring math or the issuance/consumption seams.
- Not the private-data moat — these are method proofs on public data by design.

## Hand-off
Produce: the diff (two files + README + optional-deps entry), a one-line confirmation each harness runs and reproduces the headline numbers, and where you placed them (`scripts/validate_real/` vs in-package) + why. Camilo brings the diff to the Cowork chat for review.
