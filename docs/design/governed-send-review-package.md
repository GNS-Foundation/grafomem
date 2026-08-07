# Governed-send loop (PR-2→6) — adversarial review package

**For:** Cowork adversarial review. **Branch:** `phase2/governed-send-loop` (pushed to origin).
**Design:** [governed-send-hitl-loop.md](governed-send-hitl-loop.md).
**HARD GATE:** nothing in this package reaches prod until this review passes — no `railway up`,
no PR-5 migration (there is none — see PR-5), no cloud-v2 deploy of the queue UI.

This is the send/HITL half of Phase 2. The identity + propose half (PR-1a/PR-0/PR-1b) is
already deployed and live (agent scored in Reputation). This package is **code-only**.

## What to review, by PR (diffs on the branch: `git diff main...phase2/governed-send-loop`)

| PR | Files | What it does | Risk |
|---|---|---|---|
| PR-2 | `tool_registry.py` | `ToolType.EMAIL_SEND` + `send_email` builtin — **send-LESS** stub (`sent:false`, `approved_to_send`). Real Gmail = PR-2b. | Low |
| PR-3 | `policy_engine.py` | `_eval_hitl` gains a `tools` filter → escalate a NAMED tool (`send_email`); backward-compatible. | Low |
| PR-5 | `orchestrator.py` `_create_hitl_request` | HITL request commits to `{tool,args,to,invoice_ref}` in `context_json` (JSONB); `action`/`resource` carry tool+recipient. **No migration** (deviation from the design's PR-5 — commit-in-JSONB avoids it). | Med |
| PR-4 | `orchestrator.py` `execute_step` | Tool-execution **escalation → HITL** + `WAITING_HITL` + step `ESCALATED` so `_run_sequential` PAUSES. Replaces the old silent inline-deny. | **HIGH** |
| PR-6 | `orchestrator.py` `execute_approved_action` + `hitl_routes.py` `attest` | On approve, execute the COMMITTED action deterministically (not the LLM re-run) + complete workflow. | **HIGH** |

## Attack these specifically

1. **PR-4 pause coordination (the #1 item).** A `send_email` escalation must NEVER execute the
   tool and must reliably pause. Verify: (a) `tool_escalated` detection via `EvaluationResult.ESCALATED`
   on `tool_gov_logs`; (b) `break` + `WAITING_HITL` + forced `final_status = ESCALATED` actually
   halts `_run_sequential` (line ~1544) for ALL workflow modes — **ROUND_ROBIN / SUPERVISOR are
   NOT covered** (only SEQUENTIAL); confirm the outreach agent is sequential and flag the others.
   (c) No path where a deny is mis-read as escalate or vice-versa.
2. **PR-6 no double-execution.** `attest` is `FOR UPDATE`-locked and one-shot (status flips to
   `approved`), but `execute_approved_action` runs OUTSIDE that txn — verify a retried/concurrent
   approve can't double-execute the send. Verify it does NOT also call `resume_workflow` (which
   would LLM-re-run with `ignore_governance=True`).
3. **The signed commitment (PR-5).** The approver signs `grafomem.hitl.approval.v1:<context_bytes>`.
   `context_bytes` now includes `proposed_action` — confirm the human is signing the EXACT
   `{tool,to,args}` that PR-6 executes (no field the executor reads that isn't in the signed bytes).
4. **Interim send-safety (PR-2).** `_exec_email_send` must be structurally incapable of sending
   (it isn't wired to any transport) even if reached un-gated.
5. **No premature outcome.** PR-6 deliberately records NO paid/default outcome at approval time
   (a send ≠ a resolution). Confirm this — recording one would corrupt CGR (Phase-0 lesson).

## Test coverage (all green, DB-free)

- `test_governed_send_pr23.py` — send_email stub is send-less; policy gates the named tool.
- `test_governed_send_pr456.py` — HITL request commits `{tool,recipient,invoice_ref}` + signs it;
  `execute_approved_action` runs the committed tool + completes, no LLM re-run.
- **Not unit-covered (needs staging):** the PR-4 `execute_step` pause end-to-end, and a real
  Ed25519 attest→execute round-trip. Recommend a staging integration test before deploy.

## PR-0 completion — propose→gate→HITL (added post-review, NEEDS REVIEW before deploy)

**Why:** the live smoke (`propose send_email → pending HITL → approve → execute`) could not be
driven on the corp tenant — `propose_action` was decision-only, and the HITL gate lived only in
`execute_step` (behind an LLM-emitted tool call; no provider registered). This closes the
design's original PR-0: `propose_action` now, after recording the decision, evaluates the
`tool_execution` policy and — on ESCALATE — creates the HITL request **deterministically** (no
LLM) via the reviewed `_create_hitl_request` (PR-5), committing `{tool,args,to,invoice_ref}`.

**Surface:** `OrchestratorService.propose_action` (`src/aml/cloud/orchestrator.py`). Synthetic
`workflow_id=propose:<invoice_ref>`, `step_id=<decision_id>`. Tests:
`test_orchestrator_propose.py::test_propose_action_escalates_send_email_to_hitl` /
`::test_propose_action_no_policy_no_hitl`.

**Review focus:** (a) the gate uses the same `EvaluationResult.ESCALATED` detection as PR-4;
(b) the synthetic `workflow_id` is only used to key the HITL row + PR-6's `execute_approved_action`
(whose `_update_workflow_status`/`_set_workflow_completed` no-op harmlessly on a non-existent
workflow row) — confirm that's acceptable vs. materializing a real workflow; (c) governance
failure records the decision but no HITL (fail-open on the GATE while the decision is still
audited) — confirm that's the intended posture.

**NOTE (Finding B still holds):** approve→execute records NO terminal outcome. So this makes the
`decide → gate → approve → send` half live-provable, but the `resolve → score` (CGR delta) is a
SEPARATE `/v1/governed/outcomes` write at resolution time — the HITL path alone won't move CGR.

## Cowork review findings — status

**Pre-deploy blockers (must clear before ANY deploy):**
- **F1 — CLEARED (no code change).** `_unsafe_dev_enabled()` (hitl_routes.py:25) returns False
  unless `UNSAFE_LOCAL_DEV=="true"` AND no prod markers. Prod: `UNSAFE_LOCAL_DEV` unset →
  False at line 33; belt-and-suspenders `RAILWAY_ENVIRONMENT`/`RAILWAY_PUBLIC_DOMAIN` present →
  False at line 38-41. Auto-register-approver is doubly unreachable in prod.
- **F2 — FIXED.** `attest` now parses `proposed_action` from the SIGNED `context_bytes`
  (`json.loads(row["context_bytes"])`), not the `context_json` column, so sign-X-execute-X holds
  by construction. Test: `test_hitl_attest_execute.py`.
- **F3 — CLEARED.** Both halves covered: (a) attest→execute round-trip
  (`test_hitl_attest_execute.py`, real Ed25519 sign → real attest → executes the signed action;
  deny/bad-sig never execute); (b) the `execute_step` PAUSE proven end-to-end against Postgres
  (`test_execute_step_hitl_pause.py`) — mock-LLM emits `send_email` → escalate → asserts
  WAITING_HITL + step ESCALATED + tool `execute()` NEVER called + 1 pending HITL request
  (action=send_email, resource=recipient).
- **F7 — CONFIRMED.** The escalation branch `break`s the tool loop (orchestrator.py:1206), so no
  further tools execute in that step after a send escalates.

**HARD GATES before PR-2b (real Gmail send) — tracked, NOT yet fixed:**
- **F4** — `execute_approved_action` marks the workflow COMPLETED even if the tool `execute`
  fails, and the attest caller ignores its return. Before a real send: gate completion on
  success + surface failure to the caller/response.
- **F5** — audit the EXECUTION firing (a gcrumb/decision when the send actually runs), not only
  the approval. Today only the approval is breadcrumbed.
- **F6** — the pause is SEQUENTIAL-mode only; ROUND_ROBIN/SUPERVISOR are not wired.

## Dependencies for deploy (AFTER review passes)

- Deploy is `railway up` (backend). **No migration** for PR-5 (JSONB). PR-2's `send_email`
  builtin needs `POST /v1/llm/tools/seed-builtins` re-run on the corp tenant.
- A registered HITL **approver** (Ed25519 key) for `cayerbe@ulissy.app`, and cloud-v2 signing
  (design doc PR-7) — the queue's Approve button can't attest without it. Out of this package.
- Parallel must-fix: encrypt `decision_records` context (session chip + ops/ROADMAP.md).
