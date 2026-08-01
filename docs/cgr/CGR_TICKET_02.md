# Claude Code Ticket #2 — CGR-v1 Scoring Engine (Grafomem)

**Repo:** `~/grafomem`  ·  **Depends on:** Ticket #1 (substrate capture) merged & producing data.
**Scope:** a **separable** scoring module `src/aml/cgr/` that reads the CGR substrate and emits a per-agent Capability-Grounded Reputation score. **Read-only over the data; no writes to decisions/outcomes; no UI; receivables only.**

> **Context (why separable):** CGR is conceptually the capability-grounded upgrade to GEIANT's **TierGate** trust tier, and its neutral *issuance* eventually belongs to the **GNS Foundation**, not the commercial product. So this module must be import-isolated: it may read Grafomem data-access (decision trail, stores) but must NOT depend on billing/portal/UI code, so it can later lift into a Foundation-governed service. Treat `src/aml/cgr/` as if it will one day be its own package.
>
> **Reference algorithm (do not reinvent):** `cgr_substrate.py` (ask Camilo) is the validated CGR-v1 logic — Beta-reputation core + capability prior + "verify-the-reviewer" calibration; it achieved corr **−0.99** vs realized default and recovers latent quality **+0.65** on synthetic receivables data. Port its logic faithfully; this ticket productionizes it against real substrate.

## Read these first
- Ticket #1 output: the recorded decision `parameters` (`invoice_ref`, `agent_handle`, `verifiability_tag`, `agent_tier`, `reason_code`, `cgr_schema`), the `cgr-outcomes` store, and `GET /v1/cgr/substrate/export`.
- `src/aml/cloud/decision_trail.py` (`query_decisions` / export), `src/aml/server/stores.py` (`StoreManager`), `src/aml/backends/interface.py` (retrieve).
- `cgr_substrate.py` (the reference math).
- `src/aml/server/scopes.py` (add a scope if needed), `tests/` (style).

## Module layout (`src/aml/cgr/`)
```
src/aml/cgr/__init__.py
src/aml/cgr/substrate.py   # load_substrate(tenant, ...) -> list[DecisionRow] joined to outcomes
src/aml/cgr/scoring.py     # PURE functions: the Beta/prior/calibration math (no I/O)
src/aml/cgr/engine.py      # orchestration: load -> score -> per-agent CGRResult
src/aml/cgr/routes.py      # GET /v1/cgr/scores[/{agent_handle}]  (read-only)
```
**Refactor note:** if Ticket #1's `/v1/cgr/substrate/export` built the decision↔outcome join inline, move that join into `src/aml/cgr/substrate.py:load_substrate()` and have the export route call it too (single source of truth). Keep the export endpoint's response shape unchanged.

## `scoring.py` — pure math (port of `cgr_substrate.py`)
Signature-level contract (keep pure, numpy allowed):
- `beta_prior(tier: float|None, k: float=4.0) -> (alpha, beta)`:
  - if `tier` present: `alpha = 1 + k*tier`, `beta = 1 + k*(1-tier)`.
  - if `tier` is None (current POC — TierGate snapshot not wired yet): **neutral prior** `alpha=beta=1.0`. (Document this; the capability ceiling below is skipped when tier is None.)
- `reviewer_weights(resolved_reviews) -> dict[reviewer_handle, weight]`: Brier calibration on resolved outcomes (paid=1/default=0), `w = clip(1 - brier/0.25, 0, 1)`; reviewers with `< MIN_REVIEWS` (default 5) get a low default weight (0.05). *(No review data yet → returns empty; engine must tolerate this.)*
- `score_agent(decisions, outcomes, reviews, tier, k=4.0) -> CGRResult`:
  - Consider the agent's **`certify` decisions tagged `judgment`** (a `rule`-reject is not a judgment call → excluded from credit/blame).
  - Verifiable calibration: each resolved certify updates Beta (paid→alpha, default→beta), full weight.
  - Reviewer-weighted early signal: for unresolved certifies with reviews, add `w*rating` / `w*(1-rating)`.
  - `E = alpha/(alpha+beta)`; `n = alpha+beta` (confidence/evidence mass); if `tier` present, clamp `E = min(E, tier + 0.02)` (capability ceiling — can't earn reputation beyond measured capability).
  - Return `CGRResult(agent_handle, cgr_score=E, confidence=n, n_resolved, n_pending, capability_tier=tier, as_of)`.

## `engine.py` — orchestration
- `compute_scores(tenant_id) -> list[CGRResult]`: `load_substrate` → group by `agent_handle` → `score_agent` per agent.
- Deterministic; no network. Cold-start honesty: an agent with no resolved outcomes returns a wide posterior (low `confidence`) — surface it as `"unproven"`, never as a confident 0 or 1.
- Provide `to_tiergate(result) -> dict` — a **documented contract** mapping `cgr_score`+`confidence` to a TierGate-style tier band (e.g. `unproven | bronze | silver | gold` by score thresholds gated on min confidence). **Do NOT write to GEIANT/TierGate** (cross-repo, out of scope) — just emit the contract dict.

## `routes.py` — read-only exposure
- `GET /v1/cgr/scores` → `{scores: [CGRResult...], as_of}` for the tenant. `require_scope(request, "decisions:read")` (or add a `cgr:read` scope in `scopes.py` if that's cleaner — flag which you chose).
- `GET /v1/cgr/scores/{agent_handle}` → single agent, incl. `tiergate` contract dict.
- Register the router where the other cloud routers are wired (follow how `create_governed_router` is mounted in `app.py`).

## Validation (the field version of the −0.99 result)
Add `src/aml/cgr/validate.py` (or a CLI subcommand) that, given a tenant's resolved substrate, reports:
- `corr(cgr_score, realized_default_rate_per_agent)` — expect strongly negative.
- naive baseline `corr(accept_rate, default)` for comparison.
- `n_agents`, `n_resolved`, coverage.
Print a short report. This is how we confirm CGR works on real data as outcomes accumulate.

## Tests (`tests/`)
- **Synthetic fixture:** reuse `cgr_substrate.py`'s generator (or a trimmed copy in `tests/`) to produce decisions+outcomes; assert `compute_scores` ranks agents so that higher `cgr_score` ⇒ lower realized default (corr < −0.7 on the fixture).
- Cold-start: agent with 0 resolved outcomes → `confidence` below the "proven" threshold and tier `unproven`.
- Tier=None path uses neutral prior and skips the ceiling clamp (no crash).
- Reviewer calibration: a high-Brier (bad) reviewer gets ~0 weight; a good reviewer high weight.
- `GET /v1/cgr/scores` returns per-agent results joined correctly from real substrate.
- Existing suite (`pytest tests/`) still green.

## Acceptance / definition of done
1. `src/aml/cgr/` is import-isolated (no imports from portal/billing/stripe/UI modules; only data-access + stdlib/numpy). A quick `grep` of its imports confirms this.
2. On the synthetic fixture, `validate` shows CGR strongly predicts default (corr < −0.7) and beats the naive baseline.
3. `GET /v1/cgr/scores` works against real Ticket-#1 substrate; unresolved agents read `unproven`.
4. `to_tiergate` contract emitted (not written anywhere).
5. New + existing tests green.

## Non-goals
- No write to GEIANT/TierGate (contract only).
- No score publishing / neutral-index API (that's the Foundation layer, later).
- No cross-domain/multi-vertical; no UI; no changes to decision/outcome capture (that's Ticket #1).
- No prompt/scaffold "elicitation" work — this is scoring over captured data only.

## Hand-off
Produce: diff summary (new `src/aml/cgr/` files + route registration), the `validate` report on the synthetic fixture, test output, and a 3-line note on any deviation + which scope you used. Camilo brings the diff to the Cowork chat for review against `claude/reputation-score-design.md` + `claude/cgr-substrate-instrumentation-spec.md`.
