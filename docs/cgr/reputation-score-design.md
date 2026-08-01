# Capability-Grounded Reputation (CGR) — A Concrete Design

*The top build thread from `claude/thesis-beyond-orchestration.md` §5. Fuses `claude/ioe-primitives.md` (reputation as the durable primitive), `claude/j-space-measurement.md` (the capability profile), and `claude/contracts-vs-reputation.md` (the verifiability axis + escrow history). Drafted July 31, 2026, grounded in ERC-8004 and Beta-reputation math. This is a design spec, not a survey. Revised Aug 1, 2026: capability ceiling changed from a hard posterior clamp to an evidence-gated ceiling (see that section) after the hard clamp was found to invert predictive validity on a noisy-tier path during Ticket #2 implementation. Canonical copy synced into the repo at `docs/cgr/reputation-score-design.md`; source of truth is the "Beyond Orchestration" claude.ai Project.*

## The one-sentence design goal

Produce a trustworthy, hard-to-game estimate of **"will this agent do a good job on task X?"** — and make that estimate most valuable exactly where completion **cannot be proven** (the unverifiable region), because that's the region escrow and pass@k can't serve (constraint from Q6).

## The core mechanism: verify the reviewer, not the task

You cannot verify an unverifiable task — by definition. But you *can* verify a **reviewer**, using the tasks that *were* verifiable. That is the bridge across the verifiability boundary, and it's the heart of CGR:

1. On **verifiable** tasks (stream P), we have ground truth *and* the subjective ratings reviewers gave those same tasks (stream R).
2. For each reviewer *j*, measure how well their ratings predicted the verifiable ground truth → a **calibration weight** wⱼ (e.g., wⱼ = 1 − BrierScoreⱼ).
3. Apply those same calibration weights to the same reviewers' ratings on **unverifiable** tasks.

Reviewers proven accurate where we *can* check are trusted where we *can't*. Sybil/fake-review farms have no verifiable-region track record → wⱼ ≈ 0 → near-zero influence.

## The math: Beta core with capability prior, calibrated evidence, and decay

Per capability dimension *d*, maintain a Beta posterior:
```
E_d = α_d / (α_d + β_d)   ∈ [0,1]      # expected score
n_d = α_d + β_d                        # confidence / evidence mass
```

**Capability prior.** From the J-Space profile, verified capability ceiling `cap_d ∈ [0,1]`:
```
α_d(0) = 1 + k · cap_d ;  β_d(0) = 1 + k · (1 − cap_d)     # k = prior strength, e.g. 2–5
```

**Capability ceiling — evidence-gated (revised Aug 1, 2026).** Enforced at report time, gated on verifiable evidence mass so it guards the thin-evidence regime without overriding proof:
```
E_d ← min( E_d , cap_d + ε + (1 − cap_d − ε) · s ),   s = clip(n_verified_d / N_lift, 0, 1)
```
- Thin evidence (`s→0`): tight ceiling `cap_d + ε` → kills overclaiming and review-farm inflation at the source (the regime the ceiling exists to guard).
- Accumulated ground truth (`s→1` by `N_lift` resolved outcomes, e.g. `N_lift ≈ 20`): ceiling lifts toward 1.0 — **verifiable evidence dominates**; direct proof overrides a stale/noisy `cap_d`.
- *Why not a hard clamp:* `min(E_d, cap_d+ε)` on the full posterior collapses CGR onto `cap_d` whenever `cap_d` is a proxy, discarding calibration. Confirmed empirically in Ticket #2 — on a noisy-tier synthetic path the hard clamp inverted `corr(CGR, default)` from −1.0 to +0.57. Where a proxy (e.g. GEIANT TierGate) stands in for `cap_d`, the evidence gate is doubly warranted.

**Evidence update with decay:**
```
α_d ← λ·α_d + Σ_i w_i·r_i ;  β_d ← λ·β_d + Σ_i w_i·(1−r_i)
w_i = verifiability_i × calibration_i × stake_i × recency_i
```
verifiability=1 for escrow/PoTE/Validation-Registry ground truth (full weight), <1 for pure feedback scaled by reviewer calibration wⱼ, stake, recency. `λ` = forgetting factor (a one-time pump decays).

**Report the posterior, not a point.** Output `(E_d, n_d, cap_d)`. 0.9 on n=5 = "promising, unproven"; 0.9 on n=5000 = "reliable". Cold-start honest: no history → wide posterior → "unproven".

## Who issues it — neutrality structure
1. **Registries** (identity/reputation/validation): neutral commons (ERC-8004). Don't own them.
2. **Capability authority** (issues C / `cap_d`): neutral third-party measurement body (the evaluator play). Self-reported capability is worthless; a lab can't rate agents on its own models.
3. **CGR aggregator** (computes the score): the business — a data-moated bureau, not a monopoly. Multiple bureaus read the same open registries; best calibration + most cross-referenced history wins. Must not be a frontier-model owner or a transacting counterparty; methodology auditable. Credit-rating-agency / Visa structure (Zone A).

## Open sub-threads
- Calibration cold-start for reviewers: minimum verifiable-task density per reviewer before unverifiable scores are trustworthy.
- Cross-dimension transfer: does calibration on verifiable coding predict accuracy on unverifiable legal judgment?
- Gaming the capability authority: inflating `cap_d` becomes the attack surface.
- `N_lift` calibration: currently heuristic (~20, aligned to the TierGate gold floor). Should ≈ resolved count at which verifiable mass overwhelms the prior mass (`≈ k+2`), adjusted for `cap_d` noise (noisier cap → smaller N_lift, trust proof sooner).
- Prototype: done — reference `cgr_substrate.py` (corr −0.99); productionized as `src/aml/cgr/` (Tickets #2/#3).

## Sources
- [ERC-8004: Trustless Agents](https://eips.ethereum.org/EIPS/eip-8004); [Beta Reputation System (Jøsang & Ismail)](https://people.cs.vt.edu/~irchen/5984/pdf/Josang-BECC02.pdf); internal `ioe-primitives.md`, `j-space-measurement.md`, `contracts-vs-reputation.md`, `j-space-product.md`.
