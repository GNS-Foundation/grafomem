# GRAFOMEM — W1 Findings (Stable Recall)

| Field | Value |
|---|---|
| **Workload** | W1 — Stable Recall |
| **Corpus** | grafomem-bench-v0.1.3 |
| **Corpus hash** | `blake2b256:3028af2d728db47c18ef6353d3881302ad527dcbdb6bc7370f9df093942b9459` |
| **Seeds** | 5 (s = 0..4) |
| **Backends** | `persistence` (floor), `vector_only` (BGE-small-en-v1.5) |
| **Date** | 2026-05-20 |

---

## F1 — Semantic retrieval clears the deployment threshold on W1 medium/hard and ties on easy.

```
Workload:       W1, difficulty = {easy, medium, hard}
Seeds:          5 (s=0..4)
Backends:       vector_only, persistence
Corpus hash:    blake2b256:3028af2d...942b9459
Budget:         512 chars (token proxy)

Primary metric (M1, mean +/- sd over seeds; CI = paired bootstrap on dM1):

  difficulty   floor M1         vector M1        dM1 (95% CI)            deployment
  easy         1.000 +/- 0.000  1.000 +/- 0.000  +0.000 [+0.000,+0.000]  N/A (floor saturated)
  medium       0.596 +/- 0.002  1.000 +/- 0.000  +0.404 [+0.402,+0.405]  Yes (1.68x >= 1.2x)
  hard         0.358 +/- 0.002  1.000 +/- 0.000  +0.642 [+0.640,+0.643]  Yes (2.79x >= 1.2x)

Vs persistence: medium 1.68x, hard 2.79x; easy 1.00x (no headroom).
Deployment:     Yes on medium/hard; N/A on easy.

Secondary metrics (budget 512):
  M2 precision   floor: easy .060 / med .032 / hard .019   vector: .060 / .052 / .051
  M3 char/fact   floor: easy 445 / med 830 / hard 1392     vector: 444 / 494 / 499

Safety checks:  Check L = N/A (no HARD_DELETE claimed); Check P = N/A (no CRYPTOGRAPHIC_PROVENANCE)
Capability gates: vector_only claims {AUDIT}; persistence claims {}. W1 needs no
                  BI_TEMPORAL/MULTI_TENANT, so no queries excluded (E1).

Caveats:        BGE-small-en-v1.5 (reference pin); Apple M-series; exact numpy cosine
                (no ANN); M3 is a char-proxy, NOT cl100k_base (doc 03 sec.3 deferred);
                budget enforced as characters.
```

**Interpretation.** On easy (horizon 10) the recency floor already saturates at M1 = 1.000, so vector has no room to improve — the bootstrap correctly reads the difference as inconclusive. The floor then decays with horizon (0.596 medium, 0.358 hard) purely because a long-ago fact has been pushed out of the recency window, while `vector_only` is recency-independent and recovers it. The win is therefore real and large on medium/hard (1.68x, 2.79x), with razor-tight CIs because the floor is structurally seed-invariant (eviction is a function of recency x horizon, not which facts were chosen). W1 confirms the floor behaves as designed and that any competent retriever beats it on long horizons; it does **not** discriminate between *good* backends — that is W2–W6's job.

---

## F2 — On W1-hard, perfect recall is achievable but wasteful; the efficient operating point is a tight (~1–2 fact) budget.

```
Workload:       W1, difficulty = hard
Seeds:          5 (s=0..4)
Backend:        vector_only (BGE-small-en-v1.5)
Corpus hash:    blake2b256:3028af2d...942b9459

Budget sweep (vector_only, hard):

  budget   ~facts   M1      M2      M3 (char/fact)
     32      1      0.801   0.864    28.9
     64      2      0.954   0.461    55.2
    128      5      0.995   0.217   115.9
    256     10      1.000   0.103   243.5
    512     20      1.000   0.051   498.7
```

**Interpretation.** Going from a 1-fact budget to a 20-fact budget buys the last 0.199 of recall (0.801 -> 1.000) at **~17x the token cost** (28.9 -> 498.7 char/correct-fact) and **~17x worse precision** (0.864 -> 0.051). At the tight end, `vector_only` delivers 80% recall at 86% precision for ~29 chars/fact — meaning BGE-small genuinely ranks the exact target **first** ~80% of the time against 500 distractors (the budget-32 result is effectively Recall@1; M2 = 0.864 confirms it, far above a lexical stub's ~0.27 at the same budget). The canonical 512-char budget is therefore the wrong operating point for a single-fact-answer workload: it floods context with ~19 irrelevant memories per query. The defensible deployment guidance from W1 is to run vector retrieval at a tight budget; perfect recall via a generous budget is an efficiency anti-pattern, not a feature.

*Note (M2 > M1 at budget 32):* M2 skips queries that returned nothing (precision is undefined on an empty return) while M1 counts them as misses, so M2 can exceed M1 at very tight budgets. This is the defined convention, not an artifact.

---

## Reproduction

```bash
python scripts/run_w1.py          # F1 table + F2 sweep (real BGE-small)
python scripts/generate_corpus.py # regenerate corpus; must report STABLE 3028af2d...
```

## Open items surfaced by W1

- **cl100k_base M3.** M3 is currently a char-proxy. Wire `tiktoken` into the runner for spec-faithful absolute tokens-per-fact (doc 03 sec.3); ratios are already valid since both backends share the unit.
- **Budget as a first-class axis.** W1 shows the budget dominates the recall/precision/cost tradeoff. Future workloads should report at >=2 budgets (a tight and a generous one) rather than a single canonical value.
- **W1 does not discriminate good backends.** Both reference adapters will likely saturate W1. Discrimination requires W2 (supersession), W3 (distractors), W5 (tenancy) — the capability-gated workloads.
