# Design: Governed send + HITL + CGR loop (Phase 2 core)

**Status:** proposed — for Camilo's approval before any core code is written.
**Author:** Claude Code, from the Phase-2 handoff brief (`ulissy-grafomem-operating-guide.md`, `grafomem-console-gap-audit.md`).
**Scope:** grafomem core (`src/aml/`) + a new send connector + cloud-v2 wiring. NOT a scripting change.

## 1. Why this doc exists

Phase-2's discipline is **govern through, not around**: an outreach to a named human must
be a *proposed action* that a governance policy gates into an **HITL request**, visible in
the console queue, that a human **approves** (cryptographically), after which the send
**executes** and the **outcome** feeds CGR. Phase 0 wrote straight to `/v1/governed/decisions`
— audited and CGR-scored, but with no HITL gate and no send. Phase 2 wants the full loop.

Reading the code (citations below) shows the endpoints exist but the loop **does not wire
together**. This doc is the honest gap map + the change set to close it, so we design the
security-sensitive gate/approve path *before* touching it rather than as we go.

## 2. Current state (code-verified)

The **orchestrator** (`/v1/orchestrator/*`) and the **CGR reputation loop**
(`/v1/governed/*`, `/v1/cgr/*`) are two disjoint subsystems that share `DecisionTrailService`
as substrate but are otherwise unconnected.

| # | Gap | Evidence |
|---|---|---|
| G1 | Orchestrated agents are invisible to CGR. Their decisions log `parameters={temperature, system_prompt}` only — no `agent_key`/`agent_handle`/`invoice_ref`. CGR groups solely by `agent_key`, so these rows are skipped and are unjoinable to outcomes. | `orchestrator.py:1144`; `cgr/engine.py:88-102`; `cgr/substrate.py:269,274,282` |
| G2 | No structured "propose action" entrypoint. Workflows take natural-language `input_text` and run the LLM; there is no call that submits a concrete `{tool, args, recipient}` action. | `orchestrator_routes.py:84,93,302` |
| G3 | HITL is created **only** by an *escalate* verdict at the **pre-inference** gate, keyed to the whole step (`action="execute_step"`, `resource=agent_id`) — no tool, no recipient. | `orchestrator.py:713-779, 1984-2005` |
| G4 | A **tool**-execution escalation (e.g. `send_email`) is silently downgraded to an inline deny — it does not create HITL or pause the workflow. | `orchestrator.py:1059-1081` |
| G5 | No policy construct for "tool == send_email to a named human". `hitl_required` matches coarse operation strings (`inference`/`tool_execution`/`output_check`); `tool_deny` can only hard-deny. | `policy_engine.py:257-265, 361`; `governance.py:45-61` |
| G6 | Approve does not close the loop. `attest` flips status, writes a breadcrumb, calls `resume_workflow` — never records an outcome, never updates CGR. And `resume_workflow` re-runs the step with `ignore_governance=True`, re-driving the LLM (non-deterministic). | `hitl_routes.py:140-243`; `orchestrator.py:1612-1683` (bypass at `:1670`) |
| G7 | No email/send tool exists. Builtins: `grafomem_retrieve/write/delete/audit`, `http_get`, `http_post`. | `tool_registry.py:88-241` |
| D1 | Approve requires an **Ed25519 signature from a registered approver** over `grafomem.hitl.approval.v1:<context_bytes>\x1f<decision>`. The console must hold/sign with an approver key, and `cayerbe@ulissy.app` must be a registered approver. Whether cloud-v2 does this today is unconfirmed. | `hitl_routes.py:140-199` |

## 3. Target end-to-end flow

```
propose_outreach(row)                     # structured, no LLM
  └─> orchestrator: create governed decision   [carries agent_key + agent_handle + invoice_ref=OUT-…]
        └─> governance gate: policy "send_email → HITL"  ⇒ ESCALATE
              └─> create HITL request  [context commits to {tool:send_email, to, subject, body_ref, invoice_ref}]
                    └─> status WAITING_APPROVAL
── console (cayerbe@ulissy.app) ──────────────────────────────────────────────
  HITL Queue shows 21 pending, each naming the recipient
  approver clicks Approve → Ed25519-sign the exact context → POST /attest
        └─> execute the COMMITTED action (deterministic; NOT an LLM re-run)
              ├─ (a) approve-to-send: mark approved_to_send, record decision-side audit
              └─ (b) full: send via connector, capture receipt
        └─> record_outcome(invoice_ref, outcome)      # when the outreach later resolves
              └─> CGR reflects it on next GET /v1/cgr/scores
```

Semantic mapping (A) is unchanged: `invoice_ref = OUT-<company>-<person>`, resolve status
→ `paid|default`. The only new requirement is that the **orchestrated** decision carries the
same `invoice_ref` so outcomes still join.

## 4. Change set (~7 changes, grouped into PRs)

### PR-1 — Orchestrated agents become CGR-visible (fixes G1)
Split into two halves once implementation began:

**PR-1a — agent carries a stable CGR identity. ✅ DONE (code-only, no prod).**
- Chose option (i): nullable `agent_key` + `agent_handle` columns on `orchestrator_agents`
  (migration `007_orchestrator_agent_cgr_identity.sql` following the `001` `ADD COLUMN IF NOT
  EXISTS` precedent + base CREATE). Threaded through `AgentDefinition`, `create_agent`
  (`agent_handle` defaults to `name`), `_row_to_agent`, `agent_to_dict`, and
  `CreateAgentRequest`. Tests: `tests/test_orchestrator_agent_cgr_identity.py` (7, incl. a
  DB create→read round-trip). No behavior change for existing agents (nullable, defaults).

**PR-1b — decisions carry the identity + `invoice_ref`. ⛔ BLOCKED on open Q2.**
- Thread `agent_key`/`agent_handle`/`invoice_ref` into `decision_trail.log(parameters=…)` at
  `orchestrator.py:~1150` (and the fallback-error log). `agent_key`/`agent_handle` are now on
  the agent, but **`invoice_ref` is per-outreach**, not per-agent — it must be carried into
  `execute_step` from the proposing caller. That requires resolving **open question #2** (a
  structured `propose_action` entrypoint that carries `{tool, args, invoice_ref}`). PR-1b lands
  with that decision. **Test (then):** an orchestrated decision + resolved outcome appears in
  `/v1/cgr/scores` under the pinned key.

### PR-2 — `send_email` connector tool (fixes G7)
- Register a `custom` webhook tool (`tool_type="custom"`, `config.webhook_url` → our send
  service) per `tool_registry.py:707-711`. The send service is new infra (small): accepts
  `{to, subject, body}` + tenant auth, calls Gmail API (OAuth), returns a signed receipt.
- **Ship order:** interim **(a)** the tool is a *no-send* stub that records intent and returns
  "approved_to_send" (Camilo sends manually); full **(b)** real Gmail send. (b) is the larger
  cost (OAuth, deliverability) — log as follow-up if > ~1 day.
- **Risk / security:** the connector holds Gmail credentials; PII (recipient, body) crosses it.
  Must never be reachable except via an approved action. **Handled as prohibited-until-approved
  by PR-4/PR-6, not by the tool itself.**

### PR-3 — Policy: gate a named tool to a named human (fixes G5)
- Extend `_eval_hitl` (`policy_engine.py:257-265`) so a `hitl_required` policy can match on
  `context.tool_name` (and optionally `context.tool_args.to` being a person) in the
  `tool_execution` operation — not just the operation string. Config e.g.
  `{"operations":["tool_execution"], "tools":["send_email"]}` ⇒ ESCALATE.
- **Risk:** policy-semantics; keep backward-compatible (empty `tools` = today's behavior).
  **Test:** policy escalates for `send_email`, allows other tools.

### PR-4 — Bridge tool-execution escalation → HITL (fixes G4, the core of "govern through")
- At `orchestrator.py:1072-1081`, distinguish **escalate** from **deny**. On escalate: call an
  (extended) `_create_hitl_request` and set `WAITING_HITL`, pausing the workflow — instead of
  appending "[Governance Error]" and continuing.
- **Risk:** HIGH — touches the pause/resume state machine and the security gate. Needs careful
  handling of the tool loop position so resume executes the *right* action.

### PR-5 — HITL request commits to the concrete action (fixes G3, enables deterministic execute)
- `_create_hitl_request` (`orchestrator.py:1973-2005`) currently stores `action="execute_step"`,
  `resource=agent_id`. Extend to persist `{tool_name, tool_args (incl. recipient), invoice_ref}`
  in the request context, and surface enough in `GET /v1/hitl/requests` (`hitl_routes.py:70-79`)
  for the console to render "send email to <person> @ <company>".
- **Security feature:** the approver's Ed25519 signature already commits to `context_bytes`
  (`hitl_routes.py:189-190`) — so putting the exact action in the context means **the human
  signs the specific send**, not a generic step. This is desirable; design the context bytes to
  be canonical + stable.
- **Risk:** moderate; changes what is signed → coordinate with cloud-v2 (D1).

### PR-6 — Close the loop on approve (fixes G6)
- In `attest_request` after a successful `approve` (`hitl_routes.py:233`): execute the
  **committed** action from the HITL context **deterministically** — do NOT rely on
  `resume_workflow`'s LLM re-run (which re-drives inference with `ignore_governance=True` and may
  emit a *different* tool call). Then, when the outreach later resolves, `record_outcome`
  (`demo_routes.py:212`) with `{invoice_ref, outcome}`; CGR updates on next read.
- **Decision:** either (i) execute the send inline in the approve path, or (ii) transition the
  workflow to a "execute committed action" resume that skips inference. Prefer a dedicated
  deterministic executor over the generic LLM resume.
- **Risk:** HIGH — this is where "approval → real send" lives; get idempotency + failure
  handling right (a failed send must not lose the approval; a retried approve must not double-send).

### PR-7 — cloud-v2 approver signing + render (fixes D1, closes the UX)
- Confirm/implement that the console signs the attest challenge with a registered approver key
  for `cayerbe@ulissy.app`, and renders the per-recipient action in the queue.
- **Dependency:** `cayerbe@ulissy.app` must be registered as an HITL approver with a key. If
  cloud-v2 lacks a signing path today, this PR adds it. **Open question — needs a cloud-v2 read.**

## 5. Sequencing & rough size

```
PR-1 (CGR identity)        S   ── independent, ship first (also helps Increment 1)
PR-3 (policy)              S   ── independent
PR-2 (connector stub 'a')  S   ── independent; full 'b' Gmail = separate, M–L
PR-5 (HITL context)        M   ── needed by PR-4/PR-6
PR-4 (escalate→HITL)       L   ── depends on PR-3, PR-5
PR-6 (approve→execute)     L   ── depends on PR-4, PR-5, PR-2
PR-7 (cloud-v2 signing)    M   ── depends on PR-5; needs cloud-v2 investigation
```
Realistically **multiple days across ≥6 PRs**, several touching security-sensitive gate/approve
code. This is platform engineering, not "adapt `ingest_front.py`".

## 6. Risks & security

- **Resume bypasses governance** (`ignore_governance=True`, `orchestrator.py:1670`) — do not
  route the approved send through the LLM resume; execute the committed action directly (PR-6).
- **The approval signature is the safety boundary.** Whatever the connector can do, it must be
  reachable only after a valid approver signature over the exact action (PR-4→PR-6). The send
  connector must have no un-gated entrypoint.
- **Idempotency / no double-send:** approve is `FOR UPDATE`-locked and one-shot
  (`hitl_routes.py:148,155`), but the *execute* step we add must be idempotent on `request_id`.
- **Append-only substrate:** nothing here is reversible; a wrong send can't be un-sent — another
  argument for shipping the no-send stub (2a) first.
- **Approver key custody:** who holds `cayerbe@ulissy.app`'s approver private key, and where does
  the console sign? (D1 / PR-7 open question.)
- **PII:** recipient + body pass through the HITL context (encrypted at rest per tenant) and the
  connector. Keep bodies out of logs.

## 7. Interim (if the loop is descoped)

Keep Phase 0's audited-decision + CGR path on the corp tenant (Increment 1): the agent exists in
Agent Studio, decisions + outcomes are in the audit trail and CGR, and **sends stay a manual
founder edge action** — the same guarantee Phase 0 shipped, minus the console HITL queue. This is
strictly less than the DoD (no 21-item queue, no in-console approve→send) but is honest and
un-fabricated. Document the full loop (this doc) as the roadmap.

## 8. Open questions (resolve before/inside these PRs)

1. **cloud-v2 approver signing (D1/PR-7):** does the console already sign attest challenges, and
   is `cayerbe@ulissy.app` a registered approver? (Needs a cloud-v2 + approver-table read.)
2. **Propose entrypoint (G2): RESOLVED — add a structured `propose_action` orchestrator call**
   (deterministic, no LLM/token cost for 21 rows; carries `{agent, tool, args:{to,subject,body},
   invoice_ref}`). This becomes a new PR-0 that PR-1b, PR-4, and PR-5 build on: it creates the
   governed decision (with `agent_key`/`agent_handle`/`invoice_ref`), evaluates the send_email
   policy, and on escalate creates the recipient-scoped HITL request — without running the LLM.
3. **Send scope:** ship 2a (approve-to-send, manual send) now, 2b (real Gmail) as follow-up? Brief
   leans yes.
4. **agent_key attachment (PR-1):** new column vs side mapping; who mints/pins the key (reuse the
   Phase-0 pinned key for `gtm-outreach-agent@ulissy` for continuity?).

## 9. Test plan (new)

- PR-1: orchestrated decision + outcome → appears in `/v1/cgr/scores` under the pinned `agent_key`.
- PR-3: `send_email` policy escalates; a non-gated tool does not.
- PR-4/PR-6 (the brief's step-9 test): **an edge-gated `send_email` creates an HITL request and is
  NOT executed until a valid approval** — assert no send/receipt before attest, exactly one after.
- Isolation on the corp tenant (mirrors `test_ulissy_ingest.py`).
- Idempotency: double-approve / retried approve does not double-send.
