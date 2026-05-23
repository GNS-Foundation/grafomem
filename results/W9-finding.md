# W9 — Cross-Session Deletion: forgetting must propagate

**Finding F18.** Stub embedder (structural result; embedder-invariant — see Mechanism),
hard, seeds 0–4. Generator: `generate_w9` (deterministic, R1). Runner: `scripts/run_w9.py`.
Locked in `grafomem-bench-v0.1.9`; W9 workload rollup
`4c58511ae32cf4515237a277e56357cfe6b6b6f69834dd29dacf7f8580f8a0e5`.

## Result

Each subject's introduce / delete / probe land in **distinct** sessions — the
delete-session is never the probe-session, the invariant that makes propagation testable. A
deleted-probe issued from a *different* session must return empty (a leak is a privacy
violation); a survivor-probe must still retrieve. Three clusters, one shared store with N
per-session handles:

```
  cluster          x-session leak   survivor recall   verdict
  propagating              0.000             1.000    PROPAGATES (clean)
  session_local            1.000             1.000    LEAKS x-session
  no_propagation               —                 —    SKIPPED (no CROSS_SESSION_PROPAGATION)
```

- x-session leak, `session_local` − `propagating`: **+1.000**, 95% CI [+1.000, +1.000]

Only `propagating` is clean on both axes.

## F18 — Cross-session deletion is a propagation guarantee orthogonal to single-store deletion: a backend can honor a local delete and keep survivors, yet leak the deleted fact across sessions.

`session_local` does everything W6's `honest_delete` does *within* the issuing session —
the deleted fact is gone there, survivors intact — and it claims `CROSS_SESSION_PROPAGATION`.
Yet probed from a different session, the deleted fact resurfaces at rate **1.000**: it
tombstoned locally and never propagated. The single-store deletion guarantee (W6) and the
cross-session propagation guarantee (W9) are **independent** — passing W6 says nothing about
W9. As in F10, the capability claim does not certify the behavior: `session_local`
advertises `CROSS_SESSION_PROPAGATION` and fails it, while leaking *identically* to a store
that makes no claim. `propagating`, which removes the fact globally, is the only clean
cluster. `no_propagation` makes no claim and is correctly **skipped** (§4.9) rather than
scored — capability honesty, not a failure.

## Mechanism

- **Structural / embedder-invariant**, exactly like W6's leak (F10): a leaked fact is one
  still present and retrievable in the store *as seen from another session*. The
  deleted-probe content makes the leak maximally tempting, but `session_local` leaks
  because the fact remains in the shared store behind only a session-local tombstone. Real
  BGE is expected to reproduce the table to three decimals (as W6 did). Confirm with
  `run_w9.py` under BGE.
- **delete-session ≠ probe-session is load-bearing.** If the probe shared the delete's
  session, `session_local`'s local tombstone would hide the leak and it would pass
  spuriously. The generator places the three events in distinct sessions (intro `j%N`,
  delete `(j+1)%N`, probe `(j+2)%N`) precisely so the probe reads through a handle that
  never saw the delete.
- Because `delete(ref)` is context-free in the interface (it carries no session), the
  runner owns a per-session dispatch replay — `interface.py` and the harness untouched — the
  same additive move W8's runner uses for importance.

## Synthesis — the privacy axis is two boundaries, and one of them propagates

W5 (tenant isolation) and W6 (single-store deletion) established the privacy axis; W9
sharpens the deletion boundary into a *propagation* guarantee. "Forget" is not a local
operation — a fact deleted in one session must be gone everywhere the same instance can be
read, or the deletion is a fiction. W9 is the realised home of `CROSS_SESSION_PROPAGATION`,
and its foil shows the precise failure mode: a store that forgets locally but not globally.

> The privacy axis, complete: **delete exactly** (W6 — no leak, no over-deletion),
> **don't cross tenants** (W5), and **propagate the forget** (W9). Each is a separate way
> to be right or wrong about what a memory is allowed to surface, and none is certified by
> a capability flag — each must be tested against ground truth.

## Reproduce

```
python scripts/run_w9.py        # stub; ~seconds.  Pass embed_fn=BGE to confirm
```

Traces: `generate_w9`, seeds 0–4, deterministic (R1). Locked in `grafomem-bench-v0.1.9`; W9
workload rollup `4c58511a…`. Adding W9 leaves the W1–W6 rollups byte-identical (verified on
regeneration).
