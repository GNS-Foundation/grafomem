# GRAFOMEM — W2 Findings (Drift & Conflict)

| Field | Value |
|---|---|
| **Workload** | W2 — Drift & Conflict (supersession) |
| **Traces** | `generate_w2`, seeds 0–4, deterministic (R1); not yet in the locked corpus (see open items) |
| **Seeds** | 5 (s = 0..4) |
| **Backends** | `persistence` (floor), `vector_only`, `supersession_chain` |
| **Embedder** | BGE-small-en-v1.5 (pinned; identical across both vector backends) |
| **Date** | 2026-05-20 |

Three-way at the canonical budget (512 chars), CURRENT queries only
(historical = N/A for all three; none claim BI_TEMPORAL):

```
  diff    backend         M1               M2      M3 (char/fact)   N/A
  easy    floor           1.000 +/- 0.000  0.054     483.8           10
  easy    vector_only     1.000 +/- 0.000  0.054     484.8           10
  easy    supersession    1.000 +/- 0.000  0.100     260.8           10
  medium  floor           0.460 +/- 0.020  0.024    1085.6           55
  medium  vector_only     1.000 +/- 0.000  0.052     498.8           55
  medium  supersession    1.000 +/- 0.000  0.052     500.2           55
  hard    floor           0.132 +/- 0.005  0.007    3800.6          375
  hard    vector_only     1.000 +/- 0.000  0.051     499.1          375
  hard    supersession    1.000 +/- 0.000  0.051     499.4          375
```

At budget 512 all three backends reach M1 = 1.000 on current queries — the
capability difference is **invisible** at a generous budget. It only appears
under budget pressure (F4).

---

## F3 — On W2 drift, `vector_only`'s recall is a budget illusion: it cannot identify the current version and collapses to chance at a tight budget.

```
Workload:       W2, difficulty = hard
Seeds:          5 (s=0..4)
Backend:        vector_only (BGE-small)

Budget sweep (vector_only, hard, CURRENT queries):
  budget    M1
     32     0.281      (~1/avg-depth; depths 2-5 -> mean(1/d) ~ 0.32)
     64     0.595
    128     0.920
    256     0.992
    512     1.000

For contrast, W1 (no drift) at budget 32: vector_only M1 = 0.801.
```

**Interpretation.** A "current" query and every version of its chain share the
same subject and predicate; the differing object is not in the query and tense
is a weak embedding signal, so "S lives in Rome" (superseded) and "S lives in
Milan" (current) are near-equidistant from "where does S live?". `vector_only`
keeps all versions, so at a 1-fact budget it picks the current one only about
1/depth of the time — chance. The high M1 at budget 512 is the **flood**: it
returns ~20 memories that happen to include the head, *alongside the stale
versions*. That is not "knowing the current value"; it is returning
confidently-wrong data (the superseded fact) next to the right one, with no
signal distinguishing them. Recall-at-generous-budget conceals a backend that
has no notion of "current." The W1->W2 tight-budget drop (0.801 -> 0.281) is the
signature of the missing capability.

---

## F4 — The `SUPERSESSION_CHAIN` capability recovers tight-budget recall (+0.585 @ budget 32), with the embedder held constant.

```
Workload:       W2, difficulty = hard
Seeds:          5 (s=0..4)
Backends:       supersession_chain vs vector_only (SAME BGE-small embedder)

Budget sweep — current-query M1:
  budget    vector_only    supersession_chain    gain
     32        0.281            0.867            +0.585
     64        0.595            0.983            +0.388
    128        0.920            1.000            +0.080
    256        0.992            1.000            +0.008
    512        1.000            1.000            +0.000

dM1(supersession - vector) @512: +0.000  95% CI [+0.000, +0.000]  (saturated)
```

**Interpretation.** `supersession_chain` calls `supersede()` to retire the old
version from the retrieval candidate set, so a current query sees exactly ONE
candidate per chain — the head — turning W2 back into an unambiguous W1-style
match. At budget 32 this lifts current-query recall from 0.281 to **0.867**
(essentially the W1 number, 0.801), a +0.585 gain attributable entirely to the
capability: the embedder is byte-identical to `vector_only`'s. The gain is
concentrated at tight budgets and vanishes at budget 512, where `vector_only`
floods and catches the head anyway — which is precisely why **budget must be a
reported axis**: a single-budget comparison would show zero difference and miss
the entire result. Even where M1 ties, supersession is cleaner: on easy it
returns half the tokens (M3 260.8 vs 484.8) and never surfaces a stale fact,
whereas `vector_only`'s budget-512 "recall 1.0" includes superseded versions.

This is the first result in the project where a **capability, not a better
model**, moves the primary metric — the direct evidence the capability-gated
benchmark was built to produce.

---

## Open items surfaced by W2

- **Historical queries remain N/A.** 375 historical (as_of) queries on hard are
  excluded for all three backends (none claim `BI_TEMPORAL`). A `bi_temporal`
  adapter that stores valid-time intervals and honors `as_of` would reclaim
  them — the second capability axis. This is the natural next adapter.
- **W2 not yet in the locked corpus.** Findings cite `generate_w2` (deterministic
  per R1) but not a corpus hash. Add `W2` to `corpus.toml` + the builder's
  `_GENERATORS` and regenerate so W2 traces get a content hash like W1's.
- **Budget as a first-class axis (reaffirmed).** As in W1, the canonical-budget
  view is misleading; report at >=2 budgets (tight + generous) by default.
- **A "staleness" / data-hygiene metric.** M1/M2 do not fully capture that
  `vector_only` returns *wrong* (superseded) facts while `supersession_chain`
  returns only current ones. A leakage-style count of superseded refs surfaced
  in current-query results would quantify this directly.
