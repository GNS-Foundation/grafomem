# GRAFOMEM — W2 Findings (Drift & Conflict)

| Field | Value |
|---|---|
| **Workload** | W2 — Drift & Conflict (supersession) |
| **Traces** | `generate_w2`, seeds 0–4, deterministic (R1); not yet in the locked corpus (see open items) |
| **Seeds** | 5 (s = 0..4) |
| **Backends** | `persistence` (floor), `vector_only`, `supersession_chain`, `bi_temporal` |
| **Embedder** | BGE-small-en-v1.5 (pinned; identical across all three vector backends) |
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

## F5 — The `BI_TEMPORAL` capability reclaims the 375 historical (as_of) queries that are N/A for every other backend, answering them on a W1-like frontier — while staying identical to `supersession_chain` on current queries.

```
Workload:       W2, difficulty = hard
Seeds:          5 (s=0..4)
Backends:       bi_temporal vs supersession_chain / vector_only (SAME BGE-small)

Current-query M1 (budget sweep):
  budget    vector_only  supersession  bi_temporal
     32        0.281         0.867         0.867
     64        0.595         0.983         0.983
    128        0.920         1.000         1.000
    512        1.000         1.000         1.000
  -> bi_temporal == supersession at every budget (identical head retrieval).

Historical-query M1 (budget sweep) — bi_temporal only (N/A elsewhere):
  budget    historical M1
     32         0.855
     64         0.980
    128         0.999
    512         1.000
  -> 375 historical queries/seed (hard), unanswerable by any other backend.
```

**Interpretation.** `bi_temporal` stores each version's valid-time interval
`[valid_from, valid_until)` and resolves `retrieve(as_of=t)` to the version
valid at `t`. On current queries (`as_of=None`) it returns open-interval heads
— exactly `supersession_chain`'s behaviour, and the two match to the digit at
every budget, confirming the temporal machinery is purely additive (no
current-query regression). On historical queries it does what no other backend
can: the `as_of` falls inside a past version's window, and the interval filter
slices each chain to the single version valid then — one candidate per (subject,
predicate), the same unambiguous structure as W1. So the historical frontier
(0.855 -> 1.000) mirrors the current frontier (0.867 -> 1.000) almost exactly:
after `as_of` resolution, "what was true at time t" is structurally identical to
"what is true now."

The reclamation is the headline. For every non-`BI_TEMPORAL` backend the 375
hard historical queries are not low-scoring — they are **unscorable** (the
harness excludes them, E1), because those backends have no representation of
"valid when." `bi_temporal` turns N/A into ~1.0. This is a capability that
changes *which questions are answerable at all*, not merely how well — a
stronger claim than F4's recall recovery.

---

## W2 synthesis — the capability ladder, embedder held constant

W2 now exercises three backends across two orthogonal capability rungs, all on
the identical BGE-small embedder:

```
                       current Q (budget 32)        historical Q
  vector_only          0.281  (version-confused)     N/A (no temporal model)
  supersession_chain   0.867  (current heads only)   N/A (discards the past)
  bi_temporal          0.867  (current heads only)   0.855 -> 1.000 (reclaimed)
```

Reading down the current column: the `SUPERSESSION_CHAIN` capability triples
tight-budget recall (0.281 -> 0.867). Reading the historical column: the
`BI_TEMPORAL` capability flips 375 queries from unanswerable to ~1.0. Neither
gain touches the model — the embedder is constant throughout. This is the
benchmark's core claim made concrete: on drift-heavy memory, what a backend
*can represent* (current-only vs valid-time intervals) dominates how well it
embeds. The ladder is monotone on this workload — `bi_temporal` is a strict
superset of `supersession_chain`, which is a strict superset of `vector_only`.

---

## Open items surfaced by W2

- **[RESOLVED by F5] Historical queries.** The 375 historical (as_of) queries
  were N/A for all current-only backends; the `bi_temporal` adapter (valid-time
  intervals + `as_of` resolution) now answers them at ~1.0 (generous budget).
  Closed.
- **Bi-temporal delete (tombstone) untested.** `bi_temporal.delete` raises —
  no W1–W6 workload issues deletes, so a tombstone (close interval, no
  successor) is unbuilt by design. A future deletion+history workload would
  exercise it; building it now would be untested.
- **W2 not yet in the locked corpus.** Findings cite `generate_w2` (deterministic
  per R1) but not a corpus hash. Add `W2` to `corpus.toml` + the builder's
  `_GENERATORS` and regenerate so W2 traces get a content hash like W1's.
- **Budget as a first-class axis (reaffirmed).** As in W1, the canonical-budget
  view is misleading; report at >=2 budgets (tight + generous) by default.
- **A "staleness" / data-hygiene metric.** M1/M2 do not fully capture that
  `vector_only` returns *wrong* (superseded) facts while `supersession_chain`
  returns only current ones. A leakage-style count of superseded refs surfaced
  in current-query results would quantify this directly.
