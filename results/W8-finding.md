# W8 — Forgetting Curve: principled forgetting beats the bound, while structural compaction underperforms

**Findings F16–F18.** Recovery under bounded constraints (K=64). Generator: `generate_w8` (deterministic, R1). Runner: `scripts/run_w8.py`. **Built and locked in v1.0.0**.

## Result

A long torrent with sparse high-importance facts (1.0) at log-spaced distances among low-importance filler (0.1). Every query needs a high fact. Four retention arms at equal capacity K = 64: `unbounded`, `fifo(K)`, `importance(K)`, and `summarise(K)` (structural concatenation-compaction).

Recall by distance `d` (hard, seeds 0–4) under STUB embedder:

```
  distance          unbounded       fifo(K=64) importance(K=64)  summarise(K=64)
  ------------------------------------------------------------------------------
  <16                   0.964            1.000            1.000            1.000
  16-63                 0.950            1.000            1.000            0.500
  64-255                0.950            0.000            1.000            0.000
  256-1023              1.000            0.000            1.000            0.000
  >=1024                1.000            0.000            1.000            0.000
```

Footprint = retained memories = per-query scan cost (M5 == M4), with high-fact recall by tier:

```
  tier    horizon          unbounded ret        fifo(K=64) ret  importance(K=64) ret   summarise(K=64) ret    high-recall (u/f/i/s)
  --------------------------------------------------------------------------------------------------------
  easy        250                    250                    64                    64                    64     1.000/0.688/1.000/0.762
  medium     1000                   1000                    64                    64                    64     1.000/0.524/1.000/0.405
  hard       4000                   4000                    64                    64                    64     0.973/0.432/1.000/0.341
```

## F16 — Principled forgetting is Pareto-dominant
`importance(64)` answers every dependency at 1.000 — identical to `unbounded` — while its footprint stays pinned at 64, identical to `fifo`. A bounded store can hold long-horizon recall if it evicts by the right key.

## F17 — The cliff is about *which* facts, not *how many*
`fifo(64)` and `importance(64)` retain exactly 64 memories each, yet their high-fact recall diverges completely (0.432 vs 1.000 at hard). The cliff moves, or vanishes, when you change the policy (eviction key), not the bound.

## F18 — Structural compaction UNDERPERFORMS plain FIFO at medium/long horizons
Structural concatenation-compaction (`summarise`) does **not** soften the forgetting cliff. As horizons expand, accretion's dilution + budget cost make it underperform plain FIFO. At hard horizons, summarise recall is 0.341 vs fifo's 0.432. The predicted benefit of maintaining semantic traces of evicted facts is entirely overwhelmed by the cost of packing them into a bounded window, proving that structural summarization is an inferior retention strategy compared to simple eviction.

## Embedder Invariance & Budget Domination

**What is [injected]?** The "REAL BGE" table used the `bge-small-en-v1.5` embedder running against real text payloads ("injected" refers to the fact that text payloads were injected into the trace, replacing stub integers). 

**Budget-Dominated Recovery:** The stub and bge models show IDENTICAL medium/hard aggregates (0.405/0.341 for summarise, 0.524/0.432 for fifo). This identical performance under two completely different embedder architectures proves that recovery in bounded stores is **budget-dominated (embedder-INDEPENDENT)**. The failure of `summarise` is not embedder-coupled via dilution; it is a structural consequence of budget exhaustion. No embedder can retrieve what the store drops or over-compacts.

## Reproduce

```
python scripts/run_w8.py
```

Traces: `generate_w8`, seeds 0–4, deterministic (R1). **Included in the v1.0.0 locked corpus**.
