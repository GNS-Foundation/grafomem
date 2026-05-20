# GRAFOMEM — W4 Findings (Long-Horizon Dependencies)

| Field | Value |
|---|---|
| **Workload** | W4 — Long-Horizon Dependencies (scale / retention) |
| **Traces** | `generate_w4`, seeds 0–4, deterministic (R1); not yet in the locked corpus (see open items) |
| **Seeds** | 5 (s = 0..4) |
| **Backends** | `vector_only` (unbounded), `bounded_vector` (K=64), `persistence` (floor) |
| **Embedder** | BGE-small-en-v1.5 (pinned) |
| **Budget** | 512 chars; horizon tiers 250 / 1000 / 4000 facts |
| **Date** | 2026-05-20 |

W4 is a long torrent of distinct-entity facts (one introduced per turn), queried
at controlled distances `d` (= facts introduced after the target). Every entity
is unique, so retrieval is unambiguous — there is no drift, no time axis, no
near-miss. The only variable is operational: across a long horizon, what does a
backend retain, and can it still answer a dependency `d` facts back?

Recall by distance (hard, budget 512):

```
  distance      vector_only   bounded(K=64)   floor
  <16              1.000          1.000        0.920
  16-63            1.000          1.000        0.000
  64-255           1.000          0.000        0.000
  256-1023         1.000          0.000        0.000
  >=1024           1.000          0.000        0.000
```

Footprint = retained memories = per-query scan cost (M5 == M4), from `audit()`:

```
  tier    horizon   vector retain   bounded retain   vec recall   bnd recall
  easy        250        250             64            1.000        0.659
  medium     1000       1000             64            1.000        0.467
  hard       4000       4000             64            1.000        0.348
```

---

## F8 — Long-horizon memory is a recall-vs-footprint tradeoff: an unbounded store pays linear cost for flat recall; a bounded store pays a recall cliff for flat cost.

```
Workload:       W4, difficulty tiers easy/medium/hard
Seeds:          5 (s=0..4)

Unbounded (vector_only):  retained = horizon (250 -> 1000 -> 4000), recall 1.000
                          at every distance and every tier.
Bounded   (K=64):         retained = 64 at every horizon; recall 1.000 within the
                          window, 0.000 beyond; overall recall 0.659 -> 0.348 as
                          the horizon grows.
```

**Interpretation.** Because every fact is a distinct entity, BGE retrieves the
target flawlessly *if it is still stored* — `vector_only` is a flat 1.000 across
all distances and tiers. The cost of that perfect recall is footprint (and the
per-query scan it implies) growing linearly with the horizon: 250 → 4000. The
bounded backend inverts the bargain — its footprint and scan cost are pinned at
K=64 regardless of horizon — but it can only answer dependencies inside its
window, so its *overall* recall erodes (0.659 → 0.348) not because it retrieves
worse but because a fixed window covers an ever-smaller fraction of a growing
history. There is no free lunch at scale: you pay in linear cost or in forgotten
distance. (For our store-and-scan backends footprint and scan cost are the same
deterministic number — retained count — so M4 and M5 collapse into one honest,
reproducible quantity, no wall-clock required.)

---

## F9 — The forgetting cliff is structural, not a retrieval failure: retention sets the answerable horizon, and the embedder is irrelevant to where the cliff falls.

```
Answerable distance, by backend (hard):
  floor (recency, no semantic retrieval)   d < ~16   (~budget / fact-size)
  bounded_vector (semantic, capacity K)    d < 64    (= K, exactly)
  vector_only (semantic, unbounded)        all d
```

**Interpretation.** The bounded backend's recall is 1.000 right up to d=63 and
0.000 from d=64 — a step at *exactly* the capacity, because an evicted memory is
simply absent; no embedder, however good, can retrieve what the store dropped.
The stub run produced the identical cliff at d=64 (the structure, not the
embedder, places it). The progression floor (~16) → bounded (64) → unbounded (∞)
shows the two things that set the answerable horizon: **semantic retrieval**
lifts the floor's budget-limited ~16 to the full retained set, and **retention
policy** sets how large that retained set is. Retrieval quality (W3's lever) is
maxed out here and contributes nothing to the cliff; this is a different axis
entirely.

---

## Cross-suite synthesis — a third lever: the operational axis

W1–W3 established two levers on agent-memory recall; W4 adds a third, orthogonal
to both:

```
  failure mode                     lever                  evidence
  recency / drift / time (W1,W2)   representational       recency->vector;
                                     CAPABILITY             supersession +0.585;
                                                            bi_temporal N/A->1.0
  distractor noise (W3)            EMBEDDING QUALITY      stub 0.175 -> BGE 0.685;
                                                            capabilities inert
  scale / long horizon (W4)        RETENTION POLICY       unbounded: flat recall,
                                     (capacity/cost)        linear cost; bounded:
                                                            flat cost, cliff at d=K
```

Capability decides *which questions are answerable in principle*; embedding
quality decides *whether signal beats noise*; retention policy decides *what you
can afford to keep*. They are independent: on W4 the embedder is perfect and the
capabilities are irrelevant, yet the backends differ enormously — because the
axis under test is cost-vs-coverage, not representation or discrimination. The
practical reading extends: diagnose drift → add a capability; diagnose confusion
→ improve the embedder; diagnose scale → choose a retention policy (and the open
question that points to the next lever: can you bound footprint *without* the
cliff? — i.e. compaction/summarization that keeps meaning at lower cost).

---

## Open items surfaced by W4

- **W4 not yet in the locked corpus.** `generate_w4` is deterministic (R1) but
  not hash-pinned. Add `W4` to `corpus.toml` + `_GENERATORS` and regenerate for
  `grafomem-bench-v0.1.6` (per-workload rollups keep W1–W3 citations stable).
- **Compaction is the missing lever.** F8 frames the tradeoff but our only
  bounded strategy is FIFO eviction. A compacting/summarizing backend — bound
  footprint by merging old facts rather than dropping them — would test whether
  the cliff can be softened (recall over distance at sub-linear footprint). This
  is the natural W5/W6 or new-backend direction.
- **50k stress tier is now feasible.** The oracle handled 4000 facts in ~0.1s
  (no deletes/supersession → cheap `active_memory`), so a 50k horizon is a
  realistic stress run to confirm the linear-footprint slope holds an order of
  magnitude further. Deferred, not blocked.
- **Importance-weighted eviction.** FIFO is the simplest policy; an
  importance- or recency-frequency-weighted window would change *which* distant
  facts survive and is worth a variant once compaction is in.
