# GRAFOMEM — Evaluation Metrics Specification v0.1.0

| Field | Value |
|---|---|
| **Status** | Draft |
| **Schema version** | 0.1.0 |
| **Last updated** | 2026-05-19 |
| **Authors** | Camilo Ayerbe Posada · Claude (engineering partner) |
| **Companion docs** | `01-workload-spec.md` (v0.1.1) · `02-backend-interface.md` (v0.1.1) |

---

## 1. Scope

This document specifies:

- The eight metrics (M1–M8) used to evaluate backend performance.
- Per-workload primary metrics and deployment thresholds.
- The always-on safety checks (Check L — deletion leakage; Check P — provenance verification).
- The W6 capability classification procedure (the only non-scalar workload).
- Cross-seed aggregation and statistical reporting.
- The findings format (F1, F2, …) used in the paper and in published artifacts.
- The persistence baseline against which every architecture is measured.

This document does **not** specify:

- Trace structure — see `01-workload-spec.md` §3.
- Backend interface — see `02-backend-interface.md`.
- Implementation of any specific evaluator — those live in `src/aml/eval/`.

---

## 2. Design principles

**E1 — Honesty over harshness.** Capability gaps are categorized as N/A, not scored as failure. A backend without `BI_TEMPORAL` is not scored on W2 pre-supersession queries; those queries are removed from its denominator. This is what B1 in `02-backend-interface.md` cashes out to in the metrics layer.

**E2 — Persistence is the floor.** Every architecture must beat the persistence baseline (last-N turns) by ≥20% on the primary metric to be considered for deployment. This is the same threshold pattern used in H3-Oscillator's LTC validation; it kept that work honest and it will keep this one honest too.

**E3 — Safety checks are binary, not gradient.** A backend either leaks deleted refs or it doesn't. A signature either verifies or it doesn't. There is no "70% of the time we honor deletion" — that's a fail. Safety-check results are reported as pass/fail with full per-instance detail, never as a scalar that can be "above threshold."

**E4 — Findings carry their caveats.** Every finding (F1, F2, …) must explicitly state: workload, difficulty, seed count, confidence interval, the safety-check status of the backend at the time of measurement, and any capability-gating that affected the score. A finding that hides any of these is malformed.

**E5 — Behavior classification is a metric type.** Not every output is a scalar. W6's behavior classification (`last_write_wins`, `merge`, etc.) is a histogram, and that histogram IS the result. The metrics framework explicitly admits this.

---

## 3. Core scoring primitives

For each `agent_query` turn `q` in workload `W` run on backend `B` with seed `s`:

```python
def score_query(
    q: Turn,
    retrieved: list[Memory],
    ref_to_fact: dict[MemoryRef, fact_id],
    tokenizer: Tokenizer,
) -> QueryScore:
    requires = set(q.requires)
    
    # Map backend refs back to fact_ids via the harness's tracking dict
    retrieved_facts = {
        ref_to_fact[m.ref] for m in retrieved
        if m.ref in ref_to_fact
    }
    
    tp = retrieved_facts & requires
    fp = retrieved_facts - requires
    fn = requires - retrieved_facts
    
    # Vacuous cases handled explicitly:
    # - requires == ∅ and retrieved == ∅: correct (recall=1, precision=1)
    # - requires == ∅ and retrieved != ∅: trap or noise scenario; M7 handles it
    # - requires != ∅ and retrieved == ∅: precision=1 (no false positives), recall=0
    recall    = len(tp) / len(requires) if requires else 1.0
    precision = len(tp) / len(retrieved_facts) if retrieved_facts else 1.0
    
    tokens_used = sum(tokenizer.count(m.content) for m in retrieved)
    
    return QueryScore(
        recall=recall,
        precision=precision,
        tokens_used=tokens_used,
        tp=tp, fp=fp, fn=fn,
    )
```

`ref_to_fact` is maintained by the harness: every `write()` returns a `MemoryRef`, and the harness records the mapping back to the `fact_id` it had at hand. This is how the harness reconciles backend-opaque refs with ground-truth facts.

**Tokenizer pinning:** The harness uses `cl100k_base` (OpenAI's widely-understood tokenizer) for M3 token accounting, regardless of which tokenizer a backend uses internally for budget enforcement. This is a pinned reference for cross-backend comparability. Backends are free to use different tokenizers for their own budget logic — the harness re-counts before reporting.

---

## 4. The metric set

### 4.1 M1 — Recall@K (primary correctness)

For each workload run, the per-query recall is aggregated across queries:

$$
M1(W, B, s) = \frac{1}{|Q_W|} \sum_{q \in Q_W} \text{recall}(q)
$$

where `K` is implicitly the backend's effective retrieval budget (`budget_tokens`). M1 is the primary correctness metric and is what most findings will be stated in terms of.

**Capability dependence:** none directly. But queries that require capabilities the backend doesn't claim are excluded from `Q_W` (E1).

**Failure mode:** M1 = 0 means the backend retrieved nothing relevant. M1 = 1 means perfect recall. The persistence floor typically lands at M1 = 0.2–0.4 depending on workload difficulty.

### 4.2 M2 — Precision@K

$$
M2(W, B, s) = \frac{1}{|Q_W|} \sum_{q \in Q_W} \text{precision}(q)
$$

M2 measures whether the backend wastes its retrieval budget on irrelevant facts. High M1 + low M2 means "the backend finds the right thing but also a lot of junk." High M1 + high M2 means "the backend is precise."

**Failure mode:** M2 < 0.3 with high M1 indicates a backend that floods context with low-value memories — the kind of thing that hurts downstream agent performance even though the recall number looks fine.

### 4.3 M3 — Tokens-per-correct-fact (efficiency)

The pooled token-efficiency:

$$
M3(W, B, s) = \frac{\sum_{q \in Q_W} \text{tokens\_used}(q)}{\sum_{q \in Q_W} |\text{TP}(q)|}
$$

Defined as a pooled ratio (not a mean of per-query ratios) so it remains well-defined when individual queries have zero TP. Lower is better. Units: tokens per correct fact recalled.

**Failure mode:** M3 is undefined if `Σ |TP| = 0`. In that case it's reported as `inf` and the backend's M1 will already be 0 — i.e., it failed correctness, no point ranking its efficiency.

**Why this matters:** M3 directly proxies inference cost. A backend with M3 = 50 tokens/fact is roughly 10× more expensive to run than M3 = 5, even if their M1 scores are identical. Findings about cost-per-recall live here.

### 4.4 M4 — Operation latency

For each operation type (`write`, `supersede`, `delete`, `retrieve`, `audit`), the harness records wall-clock duration per call. Reported as P50, P95, P99 per operation type per workload.

```
M4(W, B, s) = {
  "write":     {p50: ..., p95: ..., p99: ...},
  "supersede": {p50: ..., p95: ..., p99: ...},
  "delete":    {p50: ..., p95: ..., p99: ...},
  "retrieve":  {p50: ..., p95: ..., p99: ...},
  "audit":     {p50: ..., p95: ..., p99: ...},
}
```

**Reporting:** Absolute milliseconds on the LAB's reference hardware (Apple M-series with MPS; specific machine fingerprint recorded in `corpus.yaml`). Normalized ratios vs. `vector_only` are also computed and reported alongside.

**Failure mode:** A backend with M4.retrieve.p99 > 10× the M1-matched competitor is operationally non-viable for production agent loops, regardless of correctness. Some findings will frame this explicitly.

### 4.5 M5 — Storage footprint

```
M5(W, B, s) = {
  "bytes_per_fact":   total_bytes / |facts|,
  "total_bytes":      total_bytes,
  "compression_ratio": uncompressed_bytes / total_bytes,
}
```

For backends that don't expose their on-disk footprint, M5 is marked N/A and the finding notes the gap. This is a real limitation of black-box services (proprietary backends in particular) and is itself a publishable observation.

### 4.6 M6 — Temporal consistency (W2-specific)

For backends claiming `BI_TEMPORAL` on W2:

The same query is run twice — once with the correct `as_of` timestamp, once with `as_of = None` (current time). The temporal consistency score is the recall *gap*:

$$
M6(W2, B, s) = \frac{1}{|Q_{W2,\text{pre}}|} \sum_{q \in Q_{W2,\text{pre}}} \big[\text{recall}(q \mid \text{as\_of}=t_q) - \text{recall}(q \mid \text{as\_of}=\text{None})\big]
$$

where `Q_{W2,pre}` is the set of pre-supersession queries in W2.

**Interpretation:** M6 > 0 means the backend genuinely uses `as_of` to retrieve historical state. M6 ≈ 0 means the backend either ignores `as_of` (always returns current) or has no temporal index (always returns the latest). M6 < 0 is pathological and indicates a bug.

**Capability dependence:** `BI_TEMPORAL` required. For backends without it, M6 is N/A.

### 4.7 M7 — Tenant isolation (W5-specific)

For W5 trap queries — queries on Tenant A whose answer exists in Tenant B's fact set:

$$
M7(W5, B, s) = 1 - \frac{|\{q \in Q_{W5,\text{trap}} : \text{leaks}(q)\}|}{|Q_{W5,\text{trap}}|}
$$

where `leaks(q)` is true if the retrieval for tenant A returned any fact whose underlying `tenant_id ≠ A`.

**Interpretation:** M7 = 1.0 means zero cross-tenant leakage. M7 < 0.95 is a privacy violation that disqualifies the backend from multi-tenant deployment regardless of other scores.

**Capability dependence:** `MULTI_TENANT` required. For backends without it, W5 itself is skipped (per `02-backend-interface.md` §7).

---

### 4.8 M8 — Isolation conformance (W10-specific)

For W10 probes — each a concurrent group submitted under the backend's `declared_policy` (§10):

$$
M8(W10, B, s) = 1 - \frac{|\{p \in P_{W10} : \text{violates}(p)\}|}{|P_{W10}|}
$$

where `violates(p)` is true if the store **over-claims** on probe `p` — its achieved isolation level, read back from the committed outcome, is strictly below its declared level — or if `p` is a §10.4 durability probe on which a committed delete is **resurrected**.

**Interpretation:** M8 = 1.0 means the store's concurrency behavior honors its declared policy on every probe: no over-claim, no revived delete. M8 < 1.0 means at least one probe where the store claims more isolation than it delivers (e.g. declares `serializable` but admits write-skew → downgraded to `snapshot`) or where it brings back a committed delete. Like Check L, a §10.4 resurrection is a durability/regulatory exposure, not a tunable parameter.

**Decomposition.** M8 aggregates the two directions the conformance suite (§8) gates separately: the **over-claim** direction on the isolation-lattice probes (achieved vs declared level, §10.5), and the **§10.4 durability** direction on resurrection probes (a committed delete must stay deleted). The achieved isolation *level* — `serializable` ≻ `snapshot` ≻ `read_committed` — is the categorical companion reported beside M8: M8 is the scalar conformance rate, while the achieved level names where on the lattice the store actually sits.

**Capability dependence:** `CONCURRENCY_CONTROL` required. For backends without it, W10 is skipped (per `02-backend-interface.md` §10), exactly as W5 is skipped without `MULTI_TENANT`. M8 is not computed in `metrics.py` (which scores single-valued retrieval); the W10 signal is categorical, so M8 is produced by the runner bridge (`eval/concurrency_runner.py`) and surfaced by `scripts/run_w10.py` and the conformance suite.

---

## 5. Always-on safety checks

These are imported from `02-backend-interface.md` §8. They produce binary pass/fail, not scalar scores, and run on every backend regardless of workload.

### 5.1 Check L — Deletion leakage

For each `fact_id` in `GroundTruth.deleted_facts`:

1. Generate K probe queries targeting the deleted fact's predicate/subject. K = 5 in v0.1.0.
2. For each probe, call `retrieve()` with a generous `budget_tokens` and zero `as_of` filter.
3. If `AUDIT` is claimed, additionally iterate `audit()`.
4. If the deleted ref appears anywhere, record a leak with `(fact_id, surface, query)`.

**Pass criterion:** Zero leaks. Any leak fails the check; the backend's `HARD_DELETE` claim is revoked and any workload results that depended on deletion are invalidated.

### 5.2 Check P — Provenance verification

For backends claiming `CRYPTOGRAPHIC_PROVENANCE`:

1. For every `Memory` returned by every `retrieve()` and `audit()` call across the entire run, the harness verifies the Ed25519 signature against the `Memory`'s `fact_id` (mapped via `ref_to_fact`) and `source.public_key`.
2. Any verification failure is recorded with `(fact_id, signature_bytes, public_key)`.
3. A `Memory` with `source.signature == None` while the capability is claimed is also a failure.

**Pass criterion:** 100% verification rate. The Check P report records the count of verifications performed; an empty report (zero verifications) is itself a failure — the backend claimed the capability but never populated the field.

---

## 6. W6 capability classification

W6 is the only workload that does not produce a scalar correctness score. Its output is a per-conflict-pair behavior classification.

For each conflict pair `(F_A, F_B)` generated by the W6 procedure:

1. Run the workload's writes per the trace.
2. After the conflict window, query for the conflicted fact's `(predicate, subject)`.
3. Inspect the backend's response, plus `audit()` output if claimed, and classify:

| Class | Criterion |
|---|---|
| `last_write_wins` | Retrieval returns only F_B (the chronologically later write). |
| `first_write_wins` | Retrieval returns only F_A (the chronologically earlier write). |
| `merge` | Retrieval returns both, with no error signal. |
| `conflict_flag` | Retrieval surfaces a structured conflict signal (exception, marker, dual-result with annotation) — requires `CONFLICT_DETECTION` capability. |
| `silent_data_loss` | Retrieval returns neither F_A nor F_B, despite both being written successfully. |
| `non_deterministic` | Across 5 seeds with identical trace inputs, the backend produces different classifications. |

**Reporting:** A histogram over the 6 classes per `(B, difficulty)`:

```
W6(B, difficulty) = {
  "last_write_wins":   0.87,
  "first_write_wins":  0.00,
  "merge":             0.00,
  "conflict_flag":     0.00,
  "silent_data_loss":  0.13,
  "non_deterministic": 0.00,
}
```

**Interpretation:** No class is universally "correct" — different applications want different semantics. Distributed agent systems typically want `conflict_flag`; append-only audit systems want `merge`; sloppy CRUD systems get `last_write_wins`. The finding states which class the backend lands in and discusses what that means for deployment scenarios.

A backend whose `non_deterministic` rate is > 0 has a consistency bug and is flagged as such regardless of which other class dominates.

---

## 7. Per-workload primary metrics and deployment thresholds

| Workload | Primary metric | Persistence baseline | Deployment threshold |
|---|---|---|---|
| **W1** Stable Recall | M1 | last-N turns, N derived from budget_tokens | M1 ≥ 1.2 × persistence M1 |
| **W2** Drift & Conflict | M1 (all queries) + M6 (pre-sup subset, BI_TEMPORAL backends) | last-N turns | M1 ≥ 1.2 × persistence M1; **and** M6 ≥ 0.3 if BI_TEMPORAL claimed |
| **W3** Distractor Noise | M1 + M2 (precision matters under noise) | last-N turns | M1 ≥ 1.2 × persistence M1; M2 ≥ 0.5 |
| **W4** Long-Horizon | M1 at the longest horizon | last-N turns (effectively zero recall at long H) | M1 ≥ 1.2 × persistence M1 (low absolute bar) |
| **W5** Multi-Tenant | M1 + M7 | "no isolation" baseline (single bucket) | M1 ≥ 1.2 × no-isolation baseline; **and** M7 ≥ 0.95 |
| **W6** Concurrent | classification | "no consistency" baseline (typically `silent_data_loss` or `last_write_wins`) | non_deterministic rate = 0; dominant class declared explicitly |
| **W7** Forgetting *(v0.2)* | TBD | TBD | TBD |
| **W8** Right to Be Forgotten *(v0.2)* | Check L pass rate | "soft delete only" baseline (fails everything) | Check L: zero leaks |
| **W10** Concurrency & Isolation *(v0.2)* | M8 (+ achieved level) | over-claimer baseline (declares `serializable`, delivers less) | M8 = 1.0 — achieved ≥ declared on every lattice probe **and** zero §10.4 resurrections; achieved level declared explicitly |

**Always required for deployment, on every workload:**

- Check L: 0 leaks if `HARD_DELETE` is claimed.
- Check P: 100% verification if `CRYPTOGRAPHIC_PROVENANCE` is claimed.
- Conformance suite: all claimed capabilities pass.

A backend that aces M1 but fails Check L is not deployable. The recall gain is irrelevant if the backend leaks deleted data — that's a regulatory exposure, not a tunable parameter.

---

## 8. Persistence baseline

The persistence baseline is a synthetic backend defined as:

```python
class PersistenceBackend(MemoryBackend):
    """The deployment floor.
    
    Stores literally the last N turns of conversation, where N is derived
    from the harness's budget_tokens at retrieve time. No semantic indexing,
    no temporal awareness, no tenant isolation.
    """
    
    def capabilities(self) -> set[Capability]:
        return set()   # claims nothing
```

`PersistenceBackend` is included in every workload run as a reference baseline. Its M1 score establishes the floor; any architecture that fails to beat it by 20% is, by definition, not contributing anything beyond what naive last-N-turns memory already provides.

**Behavior on capability-gated workloads:**

- W2: M6 is N/A (no BI_TEMPORAL). M1 still computed; typically very low because persistence has no notion of supersession.
- W5: M7 is N/A (no MULTI_TENANT). W5 isn't run on PersistenceBackend; instead the baseline for W5 is a `NoIsolationBackend` that does store memories but ignores `tenant_id`.
- W6: classifies as `last_write_wins` or `silent_data_loss` depending on the buffer policy.
- W8: classifies as full leakage on Check L.

---

## 9. Cross-seed aggregation

For each `(B, W, difficulty)` tuple, the harness runs 5 seeds. For each metric:

```
mean   = (1/n) * Σ m_i
stddev = sqrt((1/(n-1)) * Σ (m_i - mean)²)
ci_95  = mean ± 1.96 * stddev / sqrt(n)
```

**Statistical comparisons** between backends use **paired bootstrap with 10,000 resamples** to construct CI for the difference. This is non-parametric and doesn't assume Gaussian distribution of metric values — which matters because metric distributions on hard workloads are often heavy-tailed.

A claim of the form "Backend X beats Y on W1" requires the 95% bootstrap CI of `M1(X) - M1(Y)` to exclude zero on the lower bound. Otherwise the finding is reported as inconclusive at this seed count.

---

## 10. Findings format

Findings are numbered (F1, F2, …) and follow this template:

```
F<n> — <one-sentence claim>

Workload:       W<x>, difficulty=<easy|medium|hard>
Seeds:          5 (s=0..4)
Backends:       <list of backends compared>
Corpus hash:    <BLAKE2b-256 corpus content hash>

Primary metric: <metric and value, with 95% CI>
Vs persistence: <improvement or N/A>
Deployment:     <Yes | No | N/A>

Secondary metrics: <any relevant M-numbers>
Safety checks:    Check L = <pass/fail>; Check P = <pass/fail/N/A>
Capability gates: <which capabilities affected the score>

Caveats:        <embedding model used, hardware fingerprint, etc.>
Interpretation: <2-4 sentences>
```

Example, the kind we expect from Phase 3:

```
F1 — vector_only baseline does not meet deployment threshold on W4-hard.

Workload:       W4, difficulty=hard
Seeds:          5
Backends:       vector_only, persistence
Corpus hash:    blake2b256:7a3f...

Primary metric: M1 = 0.42 ± 0.04 (CI [0.38, 0.46])
Vs persistence: 0.42 vs 0.31 → +35% improvement
Deployment:     No

Secondary metrics: M3 = 84 tokens/fact (4× persistence); M2 = 0.31
Safety checks:    Check L = N/A (no HARD_DELETE claimed); Check P = N/A
Capability gates: vector_only claims {AUDIT}; no BI_TEMPORAL, so as_of queries N/A

Caveats:        BGE-small-en-v1.5 embeddings (reference pin); Apple M-series; cl100k_base tokenizer
Interpretation: vector_only beats persistence by 35% on M1, clearing the 20% threshold for
                correctness. But M2 = 0.31 means context is 69% filler; M3 confirms 4× the
                token-per-fact cost of the trivial baseline. The architectural delta is real
                but the efficiency cost makes it operationally unfavorable. A graph-aware
                backend at comparable M1 with M2 > 0.5 would dominate.
```

---

## 11. Open decisions

Three to lock before implementation:

1. **Tokenizer pin for M3.** Currently set to `cl100k_base`. Alternative: claude tokenizer (proprietary, harder to reproduce externally) or `tiktoken/o200k_base` (newer OpenAI). My instinct: stick with `cl100k_base` — widely understood, stable, no proprietary surface. Worth your call given the GNS protocol's typical reproducibility stance.

2. **Statistical method for paired comparisons.** Specified as bootstrap with 10K resamples. Alternative: paired t-test with FDR correction across the 6 workloads. Bootstrap is safer but slower; t-test is faster and aligns with most ML literature. My instinct: bootstrap. The runtime cost is trivial relative to running the workloads themselves.

3. **What constitutes a "regression" on secondary metrics?** The deployment threshold is ≥20% on primary metric. But if a backend wins M1 by 25% and is 12× slower on M4, does it deploy? Specifying a hard rule (e.g., "no more than 5× regression on any secondary metric") would be cleanest. My instinct: 5× regression rule on M3, M4.p99, and M5. Findings note the regression explicitly when it exists.

---

## 12. Next deliverables

Phase 0 specs are now complete. Phase 1 begins:

1. `src/aml/generator/trace.py` — Python dataclasses + JSON-Schema validators, schema v0.1.1.
2. `src/aml/generator/oracle.py` — ground-truth derivation (active_memory, deleted_facts, superseded_chains).
3. `src/aml/generator/workloads/w1.py` — first workload implementation.
4. `src/aml/backends/interface.py` — Protocol + types + exception classes.
5. `src/aml/backends/persistence.py` — the baseline.
6. `src/aml/backends/vector_only.py` — first reference adapter (pinned BGE-small-en-v1.5).
7. `src/aml/eval/metrics.py` — M1–M7 implementations.
8. `src/aml/eval/harness.py` — orchestrates: generate corpus → run conformance → run workload → score → report.
9. Smoke benchmark green: 1 seed × W1 × easy × `{persistence, vector_only}` in <60s on M-series.
10. Checkpoint commit; Phase 0 → Phase 1 transition.

---

*End of document.*

*Phase 0 specifications complete. The three companion docs — workload (01), interface (02), metrics (03) — together define what GRAFOMEM measures, on what data, against which contract. Any contradiction or gap across the three is a spec bug, not an implementation detail.*
