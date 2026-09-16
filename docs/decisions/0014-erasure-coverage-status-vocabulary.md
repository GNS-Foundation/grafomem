# 0014 — Erasure-certificate coverage status vocabulary (spec-first)

**Status:** proposed 2026-09-16 · **Relates to:** [0013](0013-erasure-certificate-issuance-requires-ledger-entry.md)
(issuance obligation); enforcement = grafomem-internal I0b stage-1 (coverage verification).

## Question

An erasure certificate's `coverage` is `{store: status}` — per-subsystem findings about whether the erased
fact is really gone. But the **status vocabulary is nowhere in the spec** — it exists only in code
(`erasure_proof.py`: `verify` treats `present`/`absent` as definitive and anything else as a coverage
gap; a comment mentions `unchecked`). And `issue_certificate` **auto-defaults** omitted coverage to
`{"primary": "absent"}`, so every current caller (which omits coverage) ships that one-subsystem,
never-probed claim. What is the status vocabulary, and is `unverified` a value?

## Decision (proposed)

Define the coverage status vocabulary in the spec:

| status | meaning | counts as |
|---|---|---|
| `present` | the fact was found **still present** in the store — erasure is **incomplete** there | verified (a failure signal) |
| `absent` | the fact was **verified not present** — erased/clean | verified |
| `unverified` | the store was **not checked** — no claim is made | **not verified** (a coverage gap) |

- Only **`present` / `absent`** are *verified* statuses (a real observation). **`unverified`** (and the
  legacy `unchecked`, treated identically) is the honest value for a subsystem that was not probed — it is
  **not** a claim of absence.
- A conformant certificate MUST assert coverage **explicitly** — it MUST NOT rely on a fabricated default.
  Unprobed subsystems are recorded as `unverified`, never silently omitted or asserted `absent`.
- **Vacuity gate (I0b gate 1):** a certificate whose coverage has **zero verified entries** (empty, or
  every entry `unverified`) is refused. `{}` and the old auto-default `{"primary":"absent"}`-with-no-probe
  are the vacuous cases 0013 forbids; a real probe that finds the primary store `absent` is a verified
  entry and is accepted.

## Consequences

- The `{"primary":"absent"}` **auto-default is removed**; callers pass real coverage (see the I0b stage-1
  design). A single verified `absent` (from an actual probe) is non-vacuous; an unprobed default is not.
- `verify` already treats non-`present`/`absent` as a gap, so `unverified` slots in without changing the
  verifier; this record just names it.
- The whitepapers' "Erasure cascade ✅ VALIDATED" overstated coverage (it was an asserted default, never a
  multi-subsystem verification) — see the failure log.

## Open

- The set of subsystems a full erasure should cover (primary / embedding / cache / decision-trail) and
  which are probeable cheaply — scoped in the I0b stage-1 design (grafomem-internal); stage-1 probes the
  primary and marks the rest `unverified`.
