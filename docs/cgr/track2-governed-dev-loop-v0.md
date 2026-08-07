# Track 2 — Governed dev loop v0 ("Grafomem builds Grafomem")

The general **governed operating agent** template, instantiated first for engineering: govern
the *dev decision* inside Grafomem on the same substrate that governs GTM outreach. The operator
(a human / Claude Code) stays the executor; Grafomem governs the **decision** and gates only the
**consequential** actions. This is the design + review record for the v0 built on
`track2/governed-dev-loop-v0`, reflecting the four Cowork Step-0 conditions.

## Architecture

```
  eng-agent proposes a dev action            (POST /v1/orchestrator/agents/{id}/propose)
        │                                      invoice_ref = a work-item id (PR#/task-id)
        ▼
  policy gate  ──────────────────────────────┐
        │  consequential (deploy/apply_migration/merge_pr) → ESCALATE
        │  low-risk (open_pr/run_ci)          → ALLOW + record
        ▼                                      │
  HITL request (pending)                       │  (recorded governed decision; no HITL)
        │                                      │
   a HUMAN approver (distinct Ed25519 signer)  │
   attests out of band  (POST .../attest)      │
        │  approve → execute the committed stub │
        ▼                                      ▼
  operator runs the REAL action through the git pipeline  (CI + branch protection + Cowork review)
        │
        ▼
  outcome recorded  (POST /v1/governed/outcomes)  → CGR score on eng-agent@ulissy
```

**The loop composes with the git pipeline; it does not bypass it.** "Approved in Grafomem" never
merges past a red CI or skips Cowork review — CI, branch protection, and review still enforce at
merge. The Grafomem approval is a *governance record + a human gate on consequential actions*, not
a merge authority. The dev-tool stubs (`ops/govern_dev.py` → `/v1/llm/tools`, `tool_type=custom`,
no `webhook_url`) are **markers**: executing one on approval is a no-op; the real deploy/merge is
the operator's, in the pipeline.

## Condition 1 (HARD) — propose ≠ attest

The bridge (`GovernedDevBridge`) and Claude Code **propose** and **record outcomes**. Approval is a
**distinct Ed25519 signer** (a human), out of band. Enforcement:

- The attest handler (`hitl_routes.attest_request`) verifies `body.signature` against the
  **registered approver's** `public_key` (`hitl_approvers`). The bridge holds only the tenant API
  key and, at most, its own key — **never** the approver's private key — so any bridge-produced
  attest fails with `401`.
- `GovernedDevBridge` exposes **no** `attest`/`approve`/`sign` method and carries **no** approver
  key (a structural guard test asserts this).

Tests: `test_bridge_cannot_self_approve_consequential_action` (bridge key → 401; impersonating the
approver's `signer_id` without the private key → 401; the **real** distinct approver → 200 + the
deploy executes) and `test_bridge_exposes_no_attest_or_approver_key`.

> Prod note: the `UNSAFE_LOCAL_DEV` auto-register bypass (`hitl_routes`) must stay **off** in prod —
> it would auto-register any signer as an approver. It is off in prod; the test asserts against a
> pre-registered approver, so the bypass is not on the tested path.

## Condition 2 — test tenant, never corp

The loop writes dev-outcome CGR data; on corp it would pollute corp's GTM substrate.
`setup_dev_loop()` **refuses** the corp tenant (`5605470cfa8e415ba418c9d8944abf9a`). The CGR-delta
and RLS proofs run on freshly-minted `devtest-*` / `engA-*` / `engB-*` tenants.

## Condition 3 — invoice_ref-as-work-item-id + receivables-vocab reuse (v0 shortcut)

v0 reuses the existing substrate: `invoice_ref` carries a dev **work-item id** (PR#/task-id), and
the outcome vocabulary is the receivables set. Same transform on both propose and outcome ⇒ CGR's
pure-equality join holds (Step-0 confirmed the write-path pseudonymizes both sides consistently).

**Phase-2 ROADMAP item — "generalize the substrate schema."** The overload (one `invoice_ref`
field + one receivables vocabulary serving two domains) is a deliberate v0 shortcut, not a design
choice. Phase 2 should introduce generic decision/outcome types (a `dimension` + a domain-typed
subject/outcome) so engineering, GTM, and future domains share the scoring math without semantic
overloading. Tracked here; no schema change in v0.

## Condition 4 — the eng → outcome mapping is **binary**

`compute_scores` (`aml.cgr.scoring.score_agent`) treats outcomes **binary, at full weight**, and
only for `decision=certify` + `verifiability_tag=judgment` (which `propose_action` already tags):

| outcome label | effect on the Beta posterior |
|---|---|
| `paid` | `α += 1` — **positive** (success), counts as **resolved** |
| `default` | `β += 1` — **negative** (failure), counts as **resolved** |
| anything else (`disputed`, `late`, `written_off`, `None`) | **unscored** — falls to `n_pending`, moves no score |

So a dev result must land on **exactly `{paid, default}`** to update CGR. `map_dev_outcome()` makes
the assignment explicit:

| dev result | → | CGR label |
|---|---|---|
| `deploy_succeeded`, `migration_applied`, `ci_passed`, `merge_landed`, `pr_merged` | → | `paid` (α, positive) |
| `deploy_failed`, `deploy_rolled_back`, `migration_failed`, `ci_failed`, `merge_reverted` | → | `default` (β, negative) |
| ambiguous / in-flight / unknown | → | `None` — **do not post** (leave pending; a mis-mapped label would falsely resolve the decision and corrupt the score — the Phase-0 synthetic-data lesson) |

`merge_landed` (positive) is defined as *merged and not reverted within the observation window*; a
later `merge_reverted` is the negative signal. Tests: `test_map_dev_outcome_binary_positive_negative`,
`test_record_outcome_none_mapping_is_noop`, and the end-to-end `test_cgr_delta_on_eng_agent_after_outcome`
(a `paid` outcome resolves the decision and lifts the score off the neutral 0.5 baseline on a test
tenant). **This mapping is brought to Cowork for explicit sign-off before it is wired against any real
eng-agent outcome.**

## Test suite (CI-gated)

`tests/test_governed_dev_loop.py` — 12 tests: policy escalates exactly the consequential set;
consequential→escalates+HITL+not-executed (×3); low-risk→recorded+no-HITL (×2); propose≠attest (the
HARD invariant) + the structural no-attest guard; the binary mapping + None-noop; CGR delta after an
outcome (test tenant); per-eng-agent RLS negative across two test tenants (self-provisioned
NOSUPERUSER NOBYPASSRLS role + FORCE-RLS on `decision_records`).

## Pipeline gate

Build → PR → CI green → **Cowork adversarial review** of the config + bridge + the propose≠attest
enforcement + compose-with-CI, **before** the loop gates any real deploy.
