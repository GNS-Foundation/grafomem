# GRAFOMEM — W3 Findings (Distractor Noise)

| Field | Value |
|---|---|
| **Workload** | W3 — Distractor Noise (semantic discrimination) |
| **Traces** | `generate_w3`, seeds 0–4, deterministic (R1); locked in `grafomem-bench-v0.1.5`, W3 workload hash `2feea9ab…` |
| **Seeds** | 5 (s = 0..4) |
| **Backends** | `persistence` (floor), `vector_only` (BGE), `vector_only` (stub lexical baseline) |
| **Embedders** | BGE-small-en-v1.5 (pinned, semantic) vs a bag-of-words stub (lexical) |
| **Date** | 2026-05-20 |

W3 is structurally W1 plus a flood of never-queried distractors, with the hard
distractors being same-subject same-object-pool *near-misses* (signal "Alice
lives in Rome", near-miss "Alice was born in Paris" — only the predicate
differs). No drift, no time axis. Haystack scales 2.0× (easy) / 4.0× (medium)
/ 7.3× (hard) the signal.

Canonical budget (512 chars), current-state queries:

```
  diff    backend             M1 recall        M2 precision
  easy    floor               0.720 +/- 0.075   0.036
  easy    vector_only (BGE)   1.000 +/- 0.000   0.051
  easy    vector_only (stub)  1.000 +/- 0.000   0.053
  medium  floor               0.080 +/- 0.029   0.004
  medium  vector_only (BGE)   1.000 +/- 0.000   0.052
  medium  vector_only (stub)  0.995 +/- 0.010   0.054
  hard    floor               0.028 +/- 0.015   0.001
  hard    vector_only (BGE)   1.000 +/- 0.000   0.050
  hard    vector_only (stub)  0.800 +/- 0.014   0.041
```

`floor` (recency) collapses under noise — recall 0.720 → 0.028 across
difficulty — because recent turns are mostly distractors. It is the precision
disaster baseline.

---

## F6 — Distractor noise is a precision tax that recall hides: at a generous budget recall saturates while precision craters.

```
Workload:       W3, difficulty = hard
Seeds:          5 (s=0..4)
Backend:        vector_only (BGE)

Hard budget sweep:
  budget    recall    precision
     32     0.685       0.744
     64     0.892       0.441
    128     0.978       0.215
    256     1.000       0.103
    512     1.000       0.050
```

**Interpretation.** As the budget grows, recall climbs to 1.000 — the signal is
always eventually retrieved — but precision falls to 0.050: the backend returns
~20 memories of which one is the signal and ~19 are distractors. An evaluation
that reports only Recall@K at a generous budget would call this a solved
workload; in fact the agent is being handed the right fact buried in noise.
Recall and precision move in opposition here (the efficient point is a *tight*
budget: at 32 chars, recall 0.685 with precision 0.744), so **M2 is the
mandatory headline for noise workloads** — recall alone is blind to the tax.
This mirrors W1's efficiency frontier (F2) but driven by distractor density
rather than horizon.

---

## F7 — On discrimination under noise, the embedder is the lever and the capability ladder is flat.

```
Workload:       W3, difficulty = hard
Seeds:          5 (s=0..4)

(a) Embedder gap — recall + precision @ budget 32 (recall@1 ~ discrimination):
                       recall    precision
    vector_only(BGE)   0.685      0.744
    vector_only(stub)  0.175      0.192
    gap                +0.510     +0.552

(b) Near-miss penalty — BGE recall@32, hard:
    W1 (no near-miss):   0.801      (canonical — consistency check)
    W3 (with near-miss): 0.685
    penalty:             -0.116

(c) Capability inertness (measured; per-seed exact equality asserted):
    budget   vector   supersession   bi_temporal   delta
       32     0.685      0.685          0.685       +0.000
      512     1.000      1.000          1.000       +0.000
    W3 carries no superseded_by links and no as_of, so supersede() is never
    dispatched and as_of is always None: supersession_chain and bi_temporal are
    bit-for-bit vector_only (run_w3 asserts v == s == t per seed). The W2
    SUPERSESSION_CHAIN lever of +0.585 (current recall@32, F4) becomes +0.000.
```

**Interpretation.** Signal and near-miss are structurally identical — same
subject, same object type — so no representational capability can separate them;
only *meaning* can. (a) A semantic embedder lifts tight-budget recall from 0.175
to 0.685 and precision from 0.192 to 0.744 — the bag-of-words stub, which sees
only the shared subject token and cannot tell "lives" from "born", collapses
toward 1/(1+near-misses). (b) Even a strong embedder pays a near-miss tax: BGE
loses 0.116 of recall@32 going from W1's unrelated facts to W3's semantically
adjacent siblings — the harder the synonymy, the higher the cost. (c) Crucially,
the capabilities that dominated W2 do nothing here: with no drift and no time,
`supersession_chain` and `bi_temporal` are bit-for-bit `vector_only`. The only
lever that moves W3 is embedding quality.

---

## Cross-suite synthesis — two orthogonal levers, and a located boundary

Across W1–W3 the benchmark isolates two independent levers on agent-memory
recall, and shows that *which one matters depends on the failure mode*:

```
  failure mode             lever that moves it       evidence
  recency horizon (W1)     capability: recency->     floor 0.358 -> vector 1.000
                           semantic retrieval          (hard, F1)
  drift / supersession     capability:               vector 0.281 -> supersession
    (W2 current)             SUPERSESSION_CHAIN          0.867 @32 (F4)
  historical / time (W2)   capability: BI_TEMPORAL   N/A -> ~1.0 (375 queries, F5)
  distractor noise (W3)    EMBEDDING QUALITY         stub 0.175 -> BGE 0.685 @32;
                                                       capabilities inert (F7)
```

- On **structural** failure modes (W1 horizon, W2 drift/time) the lever is
  representational **capability** — recency vs vector, current-only vs
  supersession vs valid-time. These reshape the candidate set; embedding quality
  helps but the capability is the additive win, and it works regardless of
  embedder.
- On the **discrimination** failure mode (W3 noise) there is nothing to reshape
  — signal and noise are structurally identical — so the lever is **embedding
  quality**, and the capability ladder is flat (+0.000).

The practical reading: **diagnose the failure mode first.** Staleness or drift in
a memory layer is fixed by adding a capability (supersession, valid-time), not a
bigger embedder; confusion among plausible look-alikes is fixed by a better
embedder, not another capability. The two are complements, not substitutes — and
a benchmark that varied only one (as most agent-memory comparisons do) would
report half the picture. This is the thesis the suite was built to demonstrate.

---

## Open items surfaced by W3

- **[RESOLVED] W3 locked in the corpus.** W3 added to `corpus.toml` +
  `_GENERATORS`; `grafomem-bench-v0.1.5` now hashes W1+W2+W3 (45 traces).
  Rollups: W1 `3028af2d…` and W2 `5a39a2eb…` (both unchanged — no drift), W3
  `2feea9ab…`; aggregate `5f74fca8…`. Closed.
- **[RESOLVED] Capability inertness measured.** F7(c) is now a measured number,
  not just a logical claim: `run_w3`'s `inertness()` runs `supersession_chain`
  and `bi_temporal` on W3 hard and asserts per-seed `v == s == t` against
  `vector_only(BGE)` — identical (0.685 @32, 1.000 @512, delta +0.000), vs the
  +0.585 the same lever gives on W2. Closed.
- **The stub is a lexical baseline, not a graded model axis.** It demonstrates
  that *some* semantic content is necessary, but the BGE↔stub gap mixes
  "semantic vs lexical" with the stub's lack of stemming. A second real embedder
  (e.g. a smaller/weaker sentence model) would turn the embedder axis into a
  graded one and sharpen the "how much quality buys how much recall" curve.
- **Near-miss vs volume contribution.** Recall@32 aggregates over all signals; a
  split isolating signals *with* same-pool near-misses from those without would
  attribute the penalty precisely to near-miss density.
