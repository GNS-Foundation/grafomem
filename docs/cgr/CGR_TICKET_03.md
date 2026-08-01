# Claude Code Ticket #3 — CGR Review Capture (Grafomem)

**Repo:** `~/grafomem`  ·  **Owner (architect):** Camilo + Cowork-chat (spec)  ·  **You:** implementer
**Base:** branch `cgr/review-capture` off `main` (`33bd54f`, Ticket #2 merged).
**Scope:** *Review capture + load only.* Add the third substrate stream — funder/analyst reviews of certifications — so the **already-built, currently-dormant** reviewer-calibration bridge lights up on real data. **No scoring-math changes** (the engine already consumes reviews), no UI, no DB migration.

> **Context (why):** CGR's differentiator is "verify the reviewer, not the task" — calibrate each reviewer on *verifiable* outcomes (Brier), then trust their ratings on *unverifiable* calls. Ticket #2 built and tested that whole path (`reviewer_weights`, `score_agent`'s early signal, `engine.compute_scores(reviews=...)`), but live scoring runs `reviews=[]` because nothing captures reviews yet. Reviews are **irreversible if not captured** (same rule as outcomes). This ticket only makes the review data land + flow into scoring.

## Step 0 — base + sync the design docs into the repo
```
git checkout main && git pull --ff-only    # ensure 33bd54f (push first if origin is behind)
git checkout -b cgr/review-capture
```
Two canonical design docs are now committed at `docs/cgr/reputation-score-design.md` and `docs/cgr/cgr-substrate-instrumentation-spec.md` (previously Project-only — the coupling gap from Ticket #2). **Read §2C of the instrumentation spec (ReviewEvent) and the "verify the reviewer" section of the design doc** before implementing; pull docstring wording from those files.

## Read these first (real files — the patterns you mirror)
- `src/aml/cloud/demo_routes.py` — the **outcomes write path you mirror**: `OutcomeEvent` model, `_outcome_metadata`, `_record_outcome` (idempotency + optional-supersede-with-append-fallback), `POST /v1/governed/outcomes[/bulk]`, `_tenant_id`, `_VALID_OUTCOMES`, `create_governed_router` (store accessor `_outcomes_backend`).
- `src/aml/cgr/substrate.py` — `ReviewEvent(invoice_ref, agent_handle, reviewer, rating)` **already defined**; the outcome-store read helpers `_tenant_outcomes`/`_latest_for`/`_sort_key`/`_effective_at`; store constants `CGR_OUTCOMES_STORE`/`CGR_OUTCOME_SCHEMA`. Add the review equivalents here.
- `src/aml/cgr/engine.py` — `compute_scores(..., reviews=...)` and `compute_scores_from_rows(..., reviews=...)` **already consume reviews** (global `reviewer_weights` from reviews on resolved invoices → per-agent early signal). You only need to feed real reviews in.
- `src/aml/backends/interface.py` — `MemoryBackend.write/supersede/audit`, `WriteOptions`, `Capability`.

## The review record (Fact-shaped, mirrors the outcome record)
Store as an append-only GMP record in a **new dedicated store `cgr-reviews`**:
- `predicate = "certification_review"`, `subject = invoice_ref` (**the join key**), `object = rating` (float in [0,1]).
- metadata extras: `reviewer_handle`, `agent_handle`, `decision_id` (optional precise referent), `source`, `cgr_schema = "cgr.review.v1"`.

> **Deliberate deviation from spec §2C** (which says `subject = decision_id`): use `subject = invoice_ref`. Reviewer calibration joins reviews → **outcomes**, and outcomes are `invoice_ref`-keyed (`engine.py` line ~52: `outcomes_by_ref.get(rv.invoice_ref)`; `ReviewEvent.invoice_ref` is the key). Keeping `subject = invoice_ref` makes the review store consistent with the outcome store and the read helpers. Preserve `decision_id` in metadata for precise attribution. Flag this in the hand-off.

## The one structural difference from outcomes (important)
Outcomes are **one-per-invoice** (latest wins → `_latest_for` matches on `subject == invoice_ref`). Reviews are **many-per-invoice** — multiple reviewers rate the same certification. So:
- The dedup/revision key is **`(invoice_ref, reviewer_handle)`**, not `invoice_ref` alone. A reviewer re-rating the same invoice supersedes *their own* prior; a different reviewer is a distinct record.
- `load_reviews` must group by `(subject, reviewer_handle)` and take the latest per pair — emitting **one `ReviewEvent` per (invoice, reviewer)**. Do **not** reuse `_latest_for` as-is (it collapses to one per invoice).

## Task A — request model + write path (`demo_routes.py`)
1. Add `ReviewEvent`-request model (name it `ReviewRecord` to avoid clashing with the substrate dataclass):
   ```python
   class ReviewRecord(BaseModel):
       invoice_ref: str
       reviewer_handle: str
       rating: float                       # [0,1]
       agent_handle: str | None = None     # who made the certification (optional; can be back-filled from the decision)
       decision_id: str | None = None
       review_date: str | None = None      # ISO; default = now
       source: str = "manual"              # funder_feed | analyst | manual
   ```
2. Add `_review_metadata(...)` (mirror `_outcome_metadata`) and `_record_review(backend, *, tenant_id, ...)` (mirror `_record_outcome`): idempotent on identical re-post by the same `(invoice_ref, reviewer_handle)`; supersede-with-append-fallback on a changed rating from the same reviewer. Validate `0.0 <= rating <= 1.0` (400 otherwise).
3. Add a `_reviews_backend()` accessor in `create_governed_router` (mirror `_outcomes_backend`, store `CGR_REVIEWS_STORE`).
4. Endpoints on the same governed router (same tenant auth): `POST /v1/governed/reviews` and `POST /v1/governed/reviews/bulk`.

## Task B — review store constants + `load_reviews` (`src/aml/cgr/substrate.py`)
- Add `CGR_REVIEWS_STORE = "cgr-reviews"`, `CGR_REVIEW_SCHEMA = "cgr.review.v1"` (re-export to `demo_routes` like the outcome constants).
- Add `_tenant_reviews(backend, tenant_id)` (mirror `_tenant_outcomes`, filter on `CGR_REVIEW_SCHEMA`).
- Add `load_reviews(store_manager, tenant_id) -> list[ReviewEvent]`: read `_tenant_reviews`, group by `(subject, reviewer_handle)`, take latest per pair by `_sort_key`, emit `ReviewEvent(invoice_ref=subject, agent_handle=<from metadata>, reviewer=reviewer_handle, rating=object)`. Stdlib-only, deps injected — **preserve import isolation** (the §3 grep must stay clean).

## Task C — wire reviews into scoring (`engine.py` + the scores route)
- `compute_scores(...)`: load reviews and pass them in — i.e. default them from `load_reviews(store_manager, tenant_id)` when the caller doesn't supply `reviews`, rather than defaulting to empty. Keep the explicit `reviews=` override (tests pass synthetic reviews directly).
- Result: `GET /v1/cgr/scores` now reflects real captured reviews automatically (no route change needed beyond the engine picking them up). Confirm the reviewer signal moves scores on unresolved certifications.

## Task D — export (additive, do NOT break the byte-identical decisions shape)
- Extend `GET /v1/cgr/substrate/export`: keep `decisions[]` + `count` **byte-identical** (the Ticket-#2 regression test must still pass on the decisions array), and add an **additive** top-level `reviews[]` array (`invoice_ref`, `reviewer_handle`, `agent_handle`, `rating`, `review_date`). Update that regression test to assert decisions[] unchanged **and** reviews[] present. (If additive feels risky, add `GET /v1/cgr/substrate/reviews` instead and say which you chose.)

## Tests (`tests/`, match Ticket #1/#2 style)
- Round-trip: `POST /v1/governed/reviews` → `load_reviews` returns it as a `ReviewEvent`.
- **Many-per-invoice:** two reviewers on the same `invoice_ref` → two distinct `ReviewEvent`s (not collapsed).
- **Revision:** same reviewer re-rates same invoice → supersedes their prior (latest per `(invoice_ref, reviewer)`); identical re-post is idempotent.
- Validation: rating outside [0,1] → 400.
- Tenant isolation: a second tenant's reviews don't leak into `load_reviews` (mirror the outcomes cross-tenant test).
- **Integration (the point of the ticket):** capture decisions + outcomes on some invoices + reviews on both resolved and unresolved → `compute_scores` yields non-empty `reviewer_weights` (a calibrated reviewer earns high weight, a miscalibrated one ~0.05) and the early signal moves an unresolved agent's score vs `reviews=[]`.
- Existing suite (`pytest tests/`) still green.

## Acceptance / definition of done
1. `POST /v1/governed/reviews[/bulk]` writes append-only reviews to `cgr-reviews`; revisions supersede per `(invoice_ref, reviewer)`.
2. `load_reviews` returns one `ReviewEvent` per `(invoice, reviewer)`, tenant-scoped; import isolation of `src/aml/cgr/` still clean (run the §3 grep).
3. `compute_scores` / `GET /v1/cgr/scores` pick up real reviews and the reviewer signal demonstrably moves scores.
4. Export exposes reviews additively without changing the decisions[] shape.
5. New + existing tests green. No DB migration (reviews in a GMP store; fields in metadata). If you think a migration is warranted, STOP and flag it.

## Non-goals
- No scoring-math change (engine/scoring already consume reviews — do not touch `scoring.py`'s math).
- No UI, no cross-domain, no J-Space capability wiring (that's a separate thread).
- No publishing of scores; substrate stays private.
- No changes to signing/gcrumbs.

## Hand-off
Produce: diff summary (files touched + new endpoints), test output, and a 3-line deviation note (incl. the `subject=invoice_ref` choice and the export decision). Camilo brings the diff to the Cowork chat for review against `docs/cgr/cgr-substrate-instrumentation-spec.md` (§2C) + `docs/cgr/reputation-score-design.md`.
