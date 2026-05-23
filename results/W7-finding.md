# W7 — Conflict Detection: resolution is a behavior class, not a score

**Findings F14–F15.** Stub embedder (structural result; embedder-invariant — see
Mechanism), hard, seeds 0–4 (×2 replays). Generator: `generate_w7` (deterministic, R1).
Runner: `scripts/run_w7.py`. Locked in `grafomem-bench-v0.1.9`; W7 workload rollup
`69c11563dc9cf97477b52964bb9c030672ea288c9dea53b30b2bd88b973fabe9`.

## Result

W7 writes two facts to one `(subject, predicate)` slot — distinct objects, both left live
(neither supersedes the other, both `valid_until=None`) — then asks one conflict query
whose `requires` is the contested pair. There is no single correct answer; *what the
backend surfaces for that slot* is the measurement. Six backends share one store, each
classified by what it returns (750 = 5 seeds × 150 conflict queries):

```
  backend          class               unstable   distribution
  merge            merge                      0   {merge:750}
  last_write_wins  last_write_wins            0   {last_write_wins:750}
  first_write_wins first_write_wins           0   {first_write_wins:750}
  silent_data_loss silent_data_loss           0   {silent_data_loss:750}
  conflict_aware   conflict_flag              0   {conflict_flag:750}
  flaky            non_deterministic        380   {last_write_wins:192, first_write_wins:178}
```

Each backend lands in exactly one §4.7 class; `conflict_aware` is the only one reaching
`conflict_flag`.

## F14 — Conflict resolution is a six-way behavior class, not a correctness score.

Two simultaneously-valid writes have no single right answer, so a scalar recall metric is
meaningless here. What a backend *does* is the finding — keep the later value
(`last_write_wins`), the earlier (`first_write_wins`), both (`merge`), neither
(`silent_data_loss`), surface the clash (`conflict_flag`), or vary across runs
(`non_deterministic`). W7 is the one workload whose output is a capability map, not a
number. The taxonomy is exhaustive and mutually exclusive: every backend lands in exactly
one class, and five of six are 750/750 stable.

## F15 — Only a `CONFLICT_DETECTION` claimant can surface a conflict; every other backend resolves it silently.

`conflict_aware` is the sole backend reaching `conflict_flag` — and it alone claims
`CONFLICT_DETECTION`. The other five hand the caller a confident answer (one value, both,
or none) with no signal that a conflict existed. `silent_data_loss` is the worst case: it
returns *neither* contested value — the collision erases the data. So without the
capability a conflict is **invisible**: the caller cannot distinguish a clean answer from a
silently-resolved clash. This is the mirror of F10. There, a backend falsely *claimed* a
capability it didn't honor; here the capability is what *enables* honesty — `conflict_flag`
requires `CONFLICT_DETECTION`, and only the claimant delivers it. Separately, `flaky` is
caught as `non_deterministic` only by **replay**: 380/750 of its answers flip between
`last_write_wins` and `first_write_wins` across two replays of the identical trace; a
single run would misclassify it as a deterministic class.

## Mechanism

- **Structural / embedder-invariant.** The contested pair is surfaced (or not) by the
  backend's resolution rule on the `(subject, predicate)` slot, not by embedding rank —
  both contested facts are top candidates by construction. The class is discrete, so there
  is no score to drift; real BGE is expected to reproduce the map exactly. Confirm with
  `run_w7.py` under BGE.
- The conflict lives entirely in the facts (two same-`(s,p)`, distinct objects, both
  open-ended, both `superseded_by=None`), so the oracle places **both** in `active_memory`
  and validators stay clean — no schema or oracle change. The contested-slot signal rides
  the `{subject, predicate}` write metadata, the same channel W6's coarse-delete uses.
- **Replay is the determinism probe.** Determinism can only be observed across runs, so the
  runner replays each trace (×2) and flags any slot whose answer changes — that, and only
  that, separates `non_deterministic` from the five stable classes.

## Synthesis — the consistency end of the representation axis

W7 sits with W2 on the representational axis, at the opposite extreme. W2 is *clean* drift:
one write supersedes another, non-overlapping windows, a single well-defined current value
— the supersession machinery working as intended. W7 is the pathological case: two values,
neither supersedes, no defined answer. Together they bracket that machinery — W2 tests that
you track the single current value correctly; W7 tests what you do when there *isn't* one.
And W7 is the realised home of `CONFLICT_DETECTION`: the capability that lets a store say
"I can't resolve this" instead of silently guessing.

## Reproduce

```
python scripts/run_w7.py        # stub; ~seconds.  Pass embed_fn=BGE to confirm
```

Traces: `generate_w7`, seeds 0–4 (×2 replays), deterministic (R1). Locked in
`grafomem-bench-v0.1.9`; W7 workload rollup `69c11563…`. Adding W7 leaves the W1–W6 rollups
byte-identical (verified on regeneration).
