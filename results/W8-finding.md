# W8 — Forgetting Curve: principled forgetting beats the bound

**Findings F16–F17.** Stub embedder (structural result; embedder-invariant — see
Mechanism), hard, seeds 0–4. Generator: `generate_w8` (deterministic, R1). Runner:
`scripts/run_w8.py`. **Built but not in the locked corpus** — the lock is W1–W7 + W9;
W8 is held out pending the retention-variant (summarise/merge) decision.

## Result

A long torrent with **bimodal importance**: sparse high-importance facts (1.0) at
log-spaced distances among low-importance filler (0.1); every query needs a high fact.
The high facts number ≤ K and *straddle* K, so a bounded store can keep all of them at
the same capacity FIFO uses — a fair, equal-footprint comparison. Three retention arms at
equal capacity K = 64: `unbounded` (`vector_only`), `fifo(K)` (`bounded_vector`), and
`importance(K)` (evicts lowest-importance first, FIFO tie-break).

Recall by distance `d` (hard, seeds 0–4):

```
  distance        unbounded   fifo(K=64)   importance(K=64)
  <16               1.000        1.000          1.000
  16-63             1.000        1.000          1.000
  64-255            1.000        0.000          1.000
  256-1023          1.000        0.000          1.000
  >=1024            1.000        0.000          1.000
```

Footprint = retained memories = per-query scan cost (M5 == M4), from `audit()`, with
high-fact recall by tier:

```
  tier    horizon   unbounded ret   fifo ret   importance ret   high-recall (u / f / i)
  easy        250        250            64            64          1.000 / 0.688 / 1.000
  medium     1000       1000            64            64          1.000 / 0.524 / 1.000
  hard       4000       4000            64            64          1.000 / 0.432 / 1.000
```

- high-fact recall, importance − fifo: **+0.312 → +0.568** as the horizon grows
- importance matches unbounded recall (1.000) at **1/62 the footprint** (64 vs 4000, hard)

Only `importance(K)` holds recall at a bounded footprint.

## F16 — Principled forgetting is Pareto-dominant: importance-weighted eviction matches an unbounded store's recall at a bounded store's footprint.

`importance(64)` answers every dependency at 1.000 — identical to `unbounded` — while its
footprint stays pinned at 64, identical to `fifo`. It beats `fifo` on recall at the same
cost (1.000 vs the 0.432 high-fact recall at hard) *and* matches `unbounded`'s recall at a
fraction of the cost (64 vs 4000 retained). It is not on a trade-off curve with either; it
dominates both. The consequence for F9: the bounded recall cliff is **not** intrinsic to
bounding. A bounded store can hold long-horizon recall — if it evicts by the right key.

## F17 — The cliff is about *which* facts, not *how many*: at identical footprint, recency-eviction and importance-eviction diverge completely.

`fifo(64)` and `importance(64)` retain exactly 64 memories each — identical capacity,
identical scan cost — yet their high-fact recall is 0.432 vs 1.000 at hard. The only
difference is the **eviction key**: `fifo` drops by age, and the old high-importance facts
are precisely the ones a long-distance query needs; `importance` drops by value, so the
high facts survive regardless of age. Retention is therefore a two-part axis — *capacity*
(how much to keep) and *policy* (what to drop) — and policy is the lever W4's F8/F9 left
unmeasured. The cliff moves, or vanishes, when you change the policy, not the bound.

## Mechanism

- The result is **structural / embedder-invariant**, like F9 (the retention cliff) and
  F10 (deletion). A high fact `importance` keeps is retrievable; one `fifo` evicted is
  simply absent — no embedder, however good, retrieves what the store dropped. Recall here
  is set by the eviction key, not by embedding quality (W3's lever), which is maxed out and
  contributes nothing.
- The numbers above are the stub run; the store/evict/retrieve semantics place the result,
  not the embedder, so real BGE is expected to reproduce the table to three decimals (as in
  W4/W6). Confirm with `run_w8.py` under BGE.
- The comparison is fair *by construction*: `n_high ≤ K` lets `importance` keep every high
  fact at `fifo`'s exact capacity, and the high facts straddle K so `fifo`'s cliff falls
  inside the queried set. The magnitude (`fifo` 0.432 at hard) scales with horizon; the
  *direction* — importance ≥ fifo, importance = unbounded — is the finding.

## Synthesis — closing the retention axis

W4 established retention as the operational axis: `unbounded` pays linear cost for flat
recall, `bounded(FIFO)` pays a recall cliff for flat cost (F8), and the cliff sits at
exactly the capacity (F9). But W4 evaluated only FIFO, leaving open whether the cliff is
intrinsic to bounding. W8 answers it:

> **The bounded recall cliff is a property of the eviction policy, not the bound.**
> Importance-weighted eviction holds unbounded-level recall (1.000) at FIFO's footprint,
> because it keeps facts by value rather than recency. Retention is a two-part axis —
> *capacity* and *policy* — and policy is the lever F8/F9 left unmeasured.

This is the structural answer to the paper's stated retention gap (FIFO was the only
bounded policy evaluated). A summarise/merge policy — compaction rather than eviction — is
a further variant on the same axis; it needs a generator that emits compactable structure
and is deliberately out of scope here, which is also why W8 is held out of the locked
corpus.

## Reproduce

```
python scripts/run_w8.py        # stub; ~seconds.  Pass embed_fn=BGE to confirm invariance
```

Traces: `generate_w8`, seeds 0–4, deterministic (R1). **Not in the locked corpus** — the
lock is W1–W7 + W9; W8 is built and green but held out pending the retention-variant
decision. Folding it in later is non-destructive (the per-workload rollup design leaves
existing hashes byte-identical on regeneration).
