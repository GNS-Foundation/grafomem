# GRAFOMEM — Workload Specification v0.1.2

| Field | Value |
|---|---|
| **Status** | Draft |
| **Schema version** | 0.1.1 |
| **Last updated** | 2026-05-23 |
| **Authors** | Camilo Ayerbe Posada · Claude (engineering partner) |

---

## Changelog

**v0.1.2 (2026-05-23)** — Realign W6 with the built deletion workload; document deferred workloads.

- §4.6 W6: *Concurrent Updates* → *Deletion & Leakage*. The spec now matches the built generator; the locked corpus and the `Workload.W6` enum value are unchanged.
- New §4.7–§4.9 (deferred): Conflict Detection, Forgetting Curve, Cross-Session Deletion — the written-up homes for the reserved `CONFLICT_DETECTION` and `CROSS_SESSION_PROPAGATION` capabilities.
- §5 R4: published suite name corrected to `grafomem-bench-v0.1.8` (the locked corpus), decoupled from the spec version.

**v0.1.1 (2026-05-19)** — Inside-session deletion as a first-class Turn effect.

- Added `Fact.sequence: u64` — strictly monotonic logical clock; not part of the content hash (§3.2).
- Added `Turn.deletes: List[fact_id]` — hard-deletion events as a Turn effect (§3.4).
- Added `GroundTruth.deleted_facts` — global deletion ledger (§3.5).
- New §3.7 — Intra-turn order of operations (Rule O1).
- New §3.8 — Chain repair under deletion (Rule O2).
- New §3.9 — Cross-scope deletion propagation (Rule O3).
- Tightened `valid_from` precision to microseconds (§3.2).
- W6 procedure now requires distinct `valid_from` and `sequence` for concurrent writes (§4.6).
- Added validation Rules V1–V5 (§7.3).
- Added W8 (Right to Be Forgotten) to deferred items (§8).
- Added design principle P6 (deletion vs. supersession) (§2).

---

## 1. Scope

This document specifies:

- The data model for a generated **trace** (single benchmark sample).
- Semantic rules governing how trace data evolves under fact introduction, supersession, and deletion.
- Six workload categories (W1–W6) and their generation procedures.
- The reproducibility contract (seeding, versioning, hashing).
- The optional LLM paraphrase layer interface.
- The on-disk corpus format and validation rules.

This document does **not** specify:

- Backend interfaces — see `02-backend-interface.md`.
- Evaluation metrics — see `03-eval-metrics.md`.
- Implementation language or library choices beyond what the contract requires.

---

## 2. Design principles

**P1 — Ground truth is template-derived.** Every fact in every trace originates from a deterministic template procedure seeded by `(workload, seed, difficulty)`. The LLM paraphrase layer (§6) restates surface form only; it cannot introduce, modify, or remove facts.

**P2 — Content-addressing for facts.** Every fact has a `fact_id` computed as `BLAKE2b-128(predicate || subject || object || valid_from)`. Identical assertions across traces collide intentionally; this makes corpus diffs meaningful and supports the provenance arc (RQ5).

**P3 — Bi-temporal semantics from day one.** Every fact carries both `valid_from` (when the world-state began) and `valid_until` (when it ended, optional). The corpus does not lose history when facts change. Backends that ignore time are still testable — they fail W2/W6 in characterized ways.

**P4 — Reproducibility before naturalism.** Any quality gain from the LLM paraphrase layer must not affect ground truth or eval outcomes. Re-running the generator with the same seed and the LLM layer disabled MUST produce byte-identical output (modulo `trace_id` and `generated_at`).

**P5 — Workloads are orthogonal.** Each W1–W6 isolates one research question. Combined workloads (e.g., drift × multi-tenant) are deferred to W9+ in a future revision.

**P6 — Deletion is distinct from supersession.** Supersession preserves history; deletion destroys it. The data model and validation rules treat these as orthogonal operations with different audit semantics. A user who *changes their mind* triggers a supersession event. A user (or regulator) who invokes the *right to be forgotten* triggers a deletion event. They are not interchangeable.

---

## 3. Data model

The data model has two parts: **structural types** (§3.1–§3.6) define what a trace contains; **semantic rules** (§3.7–§3.9) define how those types evolve and interact. Both are normative.

### 3.1 Trace

```
Trace {
  schema_version:     "0.1.1"
  trace_id:           UUID                # random, non-load-bearing
  workload:           enum { W1..W6 }
  difficulty:         enum { easy, medium, hard }
  seed:               u64
  generator_version:  str                 # semver
  generated_at:       ISO8601

  facts:              List[Fact]          # final-state world model
  sessions:           List[Session]
  ground_truth:       GroundTruth
  paraphrase_meta:    Optional[ParaphraseMeta]
}
```

`facts` reflects the *final* state of the world after all turn effects have been applied (i.e., post-supersession chain repair, post-deletion). Intermediate states are reconstructible from `sessions` + `ground_truth`.

### 3.2 Fact

```
Fact {
  fact_id:        bytes16              # BLAKE2b-128(predicate || subject || object || valid_from)
  sequence:       u64                  # strictly monotonic logical clock, unique per trace
  predicate:      str                  # controlled vocabulary, §3.6
  subject:        str                  # entity reference
  object:         str | Number | bool
  valid_from:     ISO8601.ffffff       # microsecond precision REQUIRED
  valid_until:    Optional[ISO8601.ffffff]
  superseded_by:  Optional[fact_id]    # forward-pointing supersession chain
  importance:     float ∈ [0, 1]       # generator-assigned, used by W3 / W4
  tenant_id:      Optional[str]        # W5 only
  source_turn_id: Optional[UUID]       # turn that introduced this fact, if any
}
```

**`sequence` is metadata for ordering, not identity.** It is NOT part of the `fact_id` hash. Two facts with the same hash inputs but different sequences are the same fact (which cannot happen in a valid trace — see Rule V2). Two facts with the same `valid_from` but different content (different hash inputs) MUST have different `sequence` values; this is how the generator orders writes that share a wall-clock instant.

**`superseded_by` points forward**: `F1.superseded_by = F2.fact_id` means F2 replaces F1. F2 has `superseded_by = None` until further superseded. Chains form a forest, not a DAG.

### 3.3 Session

```
Session {
  session_id:  UUID
  tenant_id:   Optional[str]
  start_time:  ISO8601.ffffff
  end_time:    ISO8601.ffffff
  turns:       List[Turn]
}
```

### 3.4 Turn

```
Turn {
  turn_id:           UUID
  role:              enum { user, agent_query, agent_response }
  content:           str                # surface text — may be paraphrased
  content_template:  str                # raw template before paraphrase (canonical)
  timestamp:         ISO8601.ffffff
  introduces:        List[fact_id]      # facts that become known at this turn
  deletes:           List[fact_id]      # facts that are HARD-deleted at this turn
  requires:          List[fact_id]      # facts needed to answer (agent_query only)
  expected_response: Optional[str]      # for agent_query turns, the correct response template
}
```

Notes:
- Backends see `content` (the surface). Ground truth refers to `content_template` for reproducibility.
- `agent_query` turns are the eval points; `user` turns introduce or delete facts; `agent_response` turns are optional reference responses for end-to-end eval (Phase 2).
- `introduces` and `deletes` MUST be disjoint within a single turn (see Rule V1).

### 3.5 GroundTruth

```
GroundTruth {
  recall_targets:       Dict[turn_id, Set[fact_id]]      # = Turn.requires for agent_query turns
  active_memory:        Dict[turn_id, Set[fact_id]]      # facts retrievable as of this turn
  superseded_chains:    Dict[fact_id, List[fact_id]]     # head -> ordered chain, post-repair
  tenant_partitions:    Dict[tenant_id, Set[fact_id]]    # for W5 isolation checks
  deleted_facts:        Dict[fact_id, ISO8601.ffffff]    # the deletion ledger
}
```

**`active_memory` derivation:** at turn T with timestamp t, in session of tenant `tid`,
```
active_memory[T] = {
  f ∈ facts |
    f.valid_from ≤ t
    AND (f.valid_until is None OR f.valid_until > t)
    AND f.tenant_id matches tid
    AND f.fact_id NOT IN deleted_facts where deleted_facts[fact_id] ≤ t
}
```

**`deleted_facts` derivation:** computed by a forward pass over all sessions in `(timestamp, sequence)` order: for each `Turn.deletes` entry, the deleted `fact_id` is added with the turn's `timestamp` as its deletion time.

### 3.6 Controlled predicate vocabulary (v0.1.x)

Twenty predicates seed the vocabulary. Extending it is a versioned event:

```
lives_in       works_at      prefers       owns          dislikes
allergic_to    speaks        born_in       married_to    parent_of
employs        manages       visits        recommends    avoids
knows          member_of     located_at    costs         scheduled_for
```

Entity populations (subject/object pools) are synthetic — generated names, places, foods, dates — never real-world data, to avoid both privacy and licensing concerns.

### 3.7 Intra-turn order of operations (Rule O1)

A turn may have three distinct effects: read (`requires`), introduce (`introduces`), and delete (`deletes`). These MUST be applied in a fixed order so that ground truth is deterministic.

**Rule O1 — Within a single turn at timestamp t, effects apply in this order:**

1. **Read** `requires` against *pre-turn state* (`active_memory` computed against state up to but excluding this turn).
2. **Apply** `introduces` — new facts enter `active_memory` with `valid_from = t` and unique `sequence` values.
3. **Apply** `deletes` — facts are added to `deleted_facts` with deletion timestamp = t; chain repair (Rule O2) runs as a side effect.

This ordering means a turn that says *"remind me what I'm allergic to, and please forget it"* correctly reads the allergy first (step 1), then deletes it (step 3). It also means a turn introducing F and then deleting F in the same turn would be a generator bug — banned at validation (Rule V1).

### 3.8 Chain repair under deletion (Rule O2)

When a fact is hard-deleted, any supersession chain referencing it MUST be repaired so that no surviving fact contains a dangling reference to a deleted `fact_id`.

Suppose the chain `F0 ← F1 ← F2 ← F3` (where `←` reads "is superseded by"; F0.superseded_by = F1.fact_id, etc.).

**Rule O2 — Chain repair is deterministic:**

| Deleted node | Repair action |
|---|---|
| **Head** (F0) | F1 becomes a chain head with no upstream reference. Sequence preserved. |
| **Middle** (F1) | F0.superseded_by is rewritten to F2.fact_id. F0.valid_until keeps its original timestamp. The deleted fact's timestamp survives as residue but its content is unrecoverable. |
| **Current tail** (F3) | F2 becomes the current tail with valid_until = None. F3's deletion timestamp lives only in `deleted_facts`, not in F2. |

**Property — content vs. timestamp residue.** Chain repair preserves all surviving facts' timestamps. The *existence* of a deletion event leaks via these timestamps (because surviving facts' validity boundaries encode that *something* happened at that instant), but the *content* of the deleted fact is unrecoverable. This matches the honest semantics of cryptographic shredding.

**Property — sequence numbers can have gaps after deletion.** When a fact is deleted, its sequence number is not reassigned. Subsequent facts retain their sequence values. Sequence gaps are valid in v0.1.1 traces.

### 3.9 Cross-scope deletion propagation (Rule O3)

Deletions are not session-local. A deletion in session S1 must propagate to every other session's view of active memory, including sessions that started before S1 in wall-clock time but contain queries whose `timestamp` falls after the deletion.

**Rule O3 — `active_memory[T]` is a function of `(timestamp, tenant_id, deleted_facts ≤ timestamp)`.**

Within a tenant, the deletion ledger is global. Across tenants, deletions of one tenant's facts do not affect another tenant's view — but the cross-tenant isolation property (W5) means another tenant's facts were never retrievable in the first place, so this distinction is moot for retrieval. The relevant point is that a deletion in `tenant_id = "A"` propagates across all of A's sessions immediately.

This rule is the one most existing backends will fail. Most do not have a cross-session deletion plane; their `delete()` operations are session-scoped or soft. The eval check derived from O3 will surface this directly.

---

## 4. Workload definitions

Each workload is a deterministic function:

```
generate(workload, seed: u64, difficulty: enum, options: Dict) -> Trace
```

The same `(workload, seed, difficulty, options)` MUST produce the same Trace bytes (modulo `trace_id` and `generated_at`).

### 4.1 W1 — Stable Recall

**Purpose:** Baseline isolation of pure retrieval at varying horizons.

**Procedure:**
1. Seed RNG with `(W1, seed, difficulty)`.
2. Sample N facts from the synthetic entity population, all with `valid_from = T0`, no `valid_until`.
3. Distribute fact-introducing turns across S sessions.
4. Insert query turns at horizons H from the introducing turn (H in turns).
5. Each query turn requires exactly one fact.

**Difficulty parameters:**

| Difficulty | N facts | S sessions | Query horizons | Total turns |
|---|---|---|---|---|
| easy | 20 | 1 | 10 | ~80 |
| medium | 100 | 5 | 10, 100 | ~600 |
| hard | 500 | 20 | 10, 100, 1000 | ~3000 |

**RQs addressed:** RQ1 (consolidation under budget).

### 4.2 W2 — Drift & Conflict

**Purpose:** Test temporal reasoning and conflict resolution when facts change over time.

**Procedure:**
1. Seed RNG with `(W2, seed, difficulty)`.
2. Generate base facts as in W1.
3. For `drift_rate%` of facts, schedule a supersession event at a later timestamp; mark the old fact with `valid_until = t_super` and add a new fact with `valid_from = t_super` and `F_old.superseded_by = F_new.fact_id`.
4. Place query turns:
   - **Pre-supersession queries** (`as_of < t_super`): require the old fact.
   - **Post-supersession queries** (`as_of ≥ t_super`): require the new fact.
   - **Ambiguous queries** (no `as_of`): require the new fact under a "latest-wins" default.

**Difficulty parameters:**

| Difficulty | drift_rate | supersession depth | ambiguous query ratio |
|---|---|---|---|
| easy | 10% | 1 | 0% |
| medium | 30% | up to 2 | 50% |
| hard | 50% | up to 4 | 80% |

**RQs addressed:** RQ2 (conflict resolution), RQ6 (temporal reasoning).

### 4.3 W3 — Distractor Noise

**Purpose:** Test retrieval precision under high noise ratio.

**Procedure:** As W1, but inject K× more facts marked `importance < 0.2`. Query turns still require `importance ≥ 0.8` facts.

**Difficulty parameters:**

| Difficulty | Signal:Noise ratio |
|---|---|
| easy | 1:5 |
| medium | 1:20 |
| hard | 1:100 |

**RQs addressed:** RQ1 (precision), RQ3 (forgetting hypothesis).

### 4.4 W4 — Long-Horizon Dependencies

**Purpose:** Test consolidation policy under budget pressure at extreme horizons.

**Procedure:** A small set of high-importance facts in session 1; queries in sessions 30–50 require them; intervening sessions contain unrelated high-volume traffic (drawn from W3-style distractor distribution).

**Difficulty parameters:** total intervening turn count between introduction and query — 1k / 10k / 50k.

**RQs addressed:** RQ1, RQ3.

### 4.5 W5 — Multi-Tenant Isolation

**Purpose:** Test privacy boundaries — Tenant A queries must not return Tenant B facts.

**Procedure:**
1. Generate T independent tenant fact-sets sharing the predicate vocabulary but with disjoint subject/object populations.
2. Interleave sessions across tenants (`session.tenant_id` set explicitly).
3. Half of queries on Tenant A are **trap queries** whose answer exists *only* in Tenant B's facts. Correct behavior: backend returns empty result or explicit "not found."

**Difficulty parameters:**

| Difficulty | tenants | trap-query rate |
|---|---|---|
| easy | 2 | 25% |
| medium | 5 | 40% |
| hard | 20 | 50% |

**RQs addressed:** RQ5 (provenance/privacy boundaries).

### 4.6 W6 — Deletion & Leakage

**Purpose:** Test the deletion half of the privacy primitive — when the agent is told to forget a fact, it must actually be gone, and *only* that fact. The boundary fails in two directions: **leakage** (a forgotten fact resurfaces — a privacy violation) and **over-deletion** (facts that were *not* deleted are dropped — a correctness violation). Paired with W5 (tenant isolation), this is the two-sided privacy boundary a protocol must enforce on the read path.

**Procedure:** (reuses the W1 vocabulary, templates, oracle, and validators unchanged)
1. Select `n_subjects` subjects from the controlled person pool. Give each subject `facts_per_subject` distinct-predicate facts, all sharing `valid_from = t0`, with trace-unique monotonic `sequence`.
2. For each subject, designate the **first** fact as the one to delete and the rest as **survivors**.
3. Emit turns in three phases:
   a. **Introduce** every fact — survivors *and* to-be-deleted — as `user` turns (`introduces=[fact_id]`).
   b. **Delete** one fact per subject, after all introductions, as a `user` turn (`content="(forget) …"`, `deletes=[fact_id]`).
   c. **Probe** (order shuffled), one pair per subject:
      - **deleted-probe** — `agent_query`, `requires=[]`, content = the deleted fact's question. Correct answer is empty; the deleted fact must not appear.
      - **survivor-probe** — `agent_query`, `requires=[F_survivor]`, content = a surviving fact's question. The survivor must still be retrievable.
4. Split turns into `n_sessions`; derive ground truth via the W1 oracle. Total queries = 2 × `n_subjects`.

**Trace contents (W6-specific).** Unlike non-deletion workloads, where `Trace.facts` holds only the surviving set, **W6 carries the full introduced tape** — survivors *and* deleted facts — because the harness must replay every write, with subject/predicate metadata, *before* the delete turns fire. Without this, a delete-by-subject backend never observes the deleted fact's subject and its over-deletion cannot be caught. The surviving set is `Trace.facts` minus `GroundTruth.deleted_facts`.

**Difficulty parameters:**

| Difficulty | subjects | facts / subject | sessions | deletions |
|---|---|---|---|---|
| easy | 10 | 2 | 1 | 10 |
| medium | 25 | 3 | 4 | 25 |
| hard | 44 | 4 | 10 | 44 |

One fact per subject is deleted (deletions = subjects), and every deleted subject keeps ≥ 1 survivor — so subject-level over-deletion is always detectable.

**Eval mode — two-sided, on the read path:**
- **Leakage** (privacy, false-positive). A query leaks if its retrieved set contains a fact deleted at or before the query's timestamp, scored against `GroundTruth.deleted_facts = {fact_id: deleted_at}`. This is operationalised as the always-on **Check L** (see `02-backend-interface.md` §8); it concentrates on the deleted-probes, whose correct answer is empty.
- **Over-deletion** (correctness, false-negative). Recall (M1) over the survivor-probes. A backend that purges more than asked — e.g. by subject — scores below 1.0 here.
- A backend **passes only if** leakage ≤ ε *and* survivor recall ≥ 1 − ε. The directions are independent: exact-fact deletion passes both; a tombstone the read path ignores leaks; delete-by-subject over-deletes. The harness dispatches `delete(ref)` only to backends declaring `HARD_DELETE`; others no-op, so the content persists and leaks — the leaky baseline.

**Oracle/validator note.** The oracle rejects any query that *requires* a deleted fact, so deleted-probes carry `requires=[]` and an empty recall target (V4 satisfied — nothing required). Leakage is therefore not a trace-validator concern; it is scored at eval time against the deletion ledger.

**RQs addressed:** RQ5 (privacy boundaries — the deletion half; paired with W5).

---

> **Workloads §4.7–§4.9 are deferred (v0.2+).** They are specified but **not** part of the locked v0.1 corpus, which is W1–W6. Numbering continues above the built suite so it never collides with the locked traces. Each is the realised home of a capability the interface currently reserves: §4.7 ↔ `CONFLICT_DETECTION`, §4.9 ↔ `CROSS_SESSION_PROPAGATION`. They become benchmarkable once each has a generator.

### 4.7 W7 — Conflict Detection *(deferred, v0.2)* — formerly "Concurrent Updates"

**Status:** Specified; no generator, not in the corpus. Maps to the reserved `CONFLICT_DETECTION` capability.

**Purpose:** Test consistency semantics when multiple paths update the same fact under temporal overlap. ("Concurrency" here is **logical** — overlapping validity windows — not parallel execution; it exercises conflict *resolution*, not isolation-level concurrency control. See the note after §4.9.)

**Procedure:**
1. Generate base facts as in W1.
2. For `concurrency_rate%` of facts, two sessions in different conceptual threads write conflicting updates whose validity windows overlap.
3. All facts retain microsecond-distinct `valid_from` and trace-unique `sequence`. "Concurrency" is encoded in **window overlap** (`valid_until` of write A is later than `valid_from` of write B, or vice versa), NOT in shared timestamps.
4. Queries are placed after the conflict window.

**Eval mode — behavior classification, not binary correctness.** Backends without consistency semantics fail predictably. Each backend's behavior classifies into one of:

- `last_write_wins` — final state matches the chronologically later write
- `first_write_wins` — final state matches the earlier write
- `merge` — both values retained in some form
- `conflict_flag` — backend surfaces the conflict to the caller
- `silent_data_loss` — neither value is retrievable
- `non_deterministic` — varies across seeds

This is the only planned workload that does not produce a scalar correctness score; its finding is a capability map. A backend without `CONFLICT_DETECTION` cannot reach the `conflict_flag` class and defaults to its observed behavior class.

**RQs addressed:** RQ2 (conflict resolution at concurrency), partial RQ6.

### 4.8 W8 — Forgetting Curve *(deferred, v0.2)* — TBD

**Status:** Procedure TBD; no generator, not in the corpus. A retention-axis workload, sibling to W4; introduces no new capability flag.

**Purpose:** Test whether a store that *decays or compacts* old facts — rather than dropping them at a hard bound — holds recall over distance at sub-linear footprint. Where W4 measures the cliff of a bounded store that **evicts**, W8 would measure the curve of one that **summarises or merges**, against its declared retention policy.

**Procedure:** TBD. (Candidate: extend W4's long-horizon dependency chains, vary the store's compaction/decay policy, and trace recall as a joint function of fact age and footprint.)

### 4.9 W9 — Cross-Session Deletion *(deferred, v0.2)* — "Right to Be Forgotten"

**Status:** No generator, not in the corpus. The cross-session extension of W6; home of the reserved `CROSS_SESSION_PROPAGATION` capability.

**Required capabilities:** `HARD_DELETE`, `CROSS_SESSION_PROPAGATION`.

**Purpose:** Test that a deletion **propagates** — a fact forgotten in one session must not resurface through any other session or replica of the same backend instance. Where W6 verifies single-store deletion (forget here, gone here), W9 verifies that "forget" is global: delete in session A, probe in session B, and the fact must be gone there too. A backend lacking `CROSS_SESSION_PROPAGATION` is skipped — it cannot make the guarantee.

**Procedure:** TBD. (Candidate: W6's introduce/delete structure, but with each deletion issued in a different session from both the introduction and the matching deleted-probe.)

---

> **Not yet specified — true concurrency control.** None of W7–W9 covers *operational* concurrency: parallel readers and writers contending for the same fact under a declared isolation level (serializable, snapshot, read-committed). W7's "concurrency" is logical window-overlap (conflict resolution); isolation-level control is a distinct axis with no workload and no reserved capability — the genuinely net-new item for a future design pass.

---

## 5. Reproducibility contract

**R1 — Deterministic generation.** `generate(W, seed, difficulty, options)` produces the same Trace bytes (excluding `trace_id` and `generated_at`) on the same generator version.

**R2 — Version pinning.** Every Trace records `schema_version` and `generator_version`. Backends declare which versions they support.

**R3 — Corpus hash.** A canonical corpus (suite of traces) has a content hash computed as `BLAKE2b-256` over sorted per-trace content hashes. Every published finding cites the corpus hash it was measured on.

**R4 — Seed registry.** A `corpus.yaml` manifest lists the exact `(workload, seed, difficulty)` tuples constituting a published benchmark suite. The current locked suite is named `grafomem-bench-v0.1.8`.

---

## 6. LLM paraphrase layer

**Optional. Off by default.** When enabled, each `content_template` is passed through an LLM to produce a more natural `content`.

**Configuration:**
- Pinned model + version (e.g., `claude-haiku-4-5-20251001`, or a documented local-model checksum).
- Pinned system prompt, versioned at `paraphrase/v1.md`.
- Temperature 0.
- Output cache keyed by `BLAKE2b(content_template || paraphrase_version || model_id)`.

**Reproducibility note:** API-hosted models can drift even at temperature 0 across server-side updates. We therefore **cache and ship** the paraphrased corpus alongside the deterministic template-only corpus. A consumer of the paraphrased corpus does not need to re-run the LLM layer; the cached outputs are part of the published artifact.

**Fact preservation requirement:** A validation step (§7.3) re-extracts facts from each paraphrased `content` and asserts the extracted set is a **superset** of the template's facts. Any paraphrase that drops or modifies a fact rejects the entire corpus.

`paraphrase_meta` records `{model_id, prompt_version, temperature, cache_key_scheme}` so a reader can audit the layer.

---

## 7. On-disk format and validation

### 7.1 Corpus layout

```
corpus/
├── corpus.yaml                  # manifest: name, version, seeds, hash, generator_version
├── traces/
│   ├── W1_s0_easy.jsonl
│   ├── W1_s1_easy.jsonl
│   ├── ...
├── ground_truth/                # parallel structure, separable for held-out eval
│   └── W1_s0_easy.gt.json
└── paraphrase_cache/            # only present if LLM layer was applied
    └── <cache_key>.txt
```

Each `traces/*.jsonl` is one Trace. For W4 hard (~50k turns), one Turn per line is permitted.

### 7.2 Schema validation

Every Trace must pass a JSON-Schema check against `schemas/trace.schema.json` before acceptance into a corpus.

### 7.3 Semantic validation

For each Trace, all of the following MUST hold:

**Reference integrity:**
- All `fact_id` references in turns and ground truth resolve to entries in `facts`, OR (for `deletes` and historical references) appear in `GroundTruth.deleted_facts`.
- For every `agent_query` turn T: `Turn.requires ⊆ GroundTruth.active_memory[turn_id]`.

**Workload-specific:**
- W5: no turn's `introduces` crosses tenant boundaries.
- W2: every `superseded_by` chain terminates (no cycles).

**Deletion semantics (new in v0.1.1):**

- **V1 — Intra-turn disjointness:** A turn MUST NOT have the same `fact_id` in both `introduces` and `deletes`.
- **V2 — Live-target deletion:** `deletes` may only reference `fact_id`s that exist at the turn's pre-turn evaluation (no deleting non-existent or already-deleted facts).
- **V3 — No dangling chain references:** For every `superseded_by` reference in the final `facts` set, the referenced `fact_id` MUST also exist in `facts`. Chain repair (Rule O2) must have been applied.
- **V4 — No queries for deleted facts:** For every `agent_query` turn T, no `fact_id` in `Turn.requires` may appear in `GroundTruth.deleted_facts` at time T.
- **V5 — Deletion ledger is derived:** `GroundTruth.deleted_facts` MUST be reproducible from a single forward pass over the trace; it is not independently authored.

**Paraphrase (if applied):**
- Re-extracted facts from each `content` ⊇ template facts from `content_template`.

### 7.4 Smoke benchmark

The generator ships with a smoke suite: 1 seed × 6 workloads × `easy` difficulty. Total runtime budget: **60 seconds on M2/M3 Apple Silicon, LLM layer off**. CI runs this on every commit. If smoke exceeds 60s, the offending workload's generation procedure is profiled and patched before merge.

---

## 8. Items deferred to v0.2 (or later)

- **Combined workloads** (drift × multi-tenant, distractor × long-horizon, etc.) — orthogonality of v0.1.x makes these straightforward to layer in.
- **Dialog-tree LLM generation** — true multi-turn naturalism, as opposed to per-utterance paraphrase.
- **Adversarial workloads** — generation procedures tuned to break specific architectures.
- **Internationalization** of the predicate vocabulary (Italian, Spanish; relevant for EU-AI-Act-adjacent positioning).
- **W7 — Forgetting Curve.** Explicit test of RQ3: workload designed such that *deliberately forgetting* low-importance facts strictly improves recall on high-importance ones. Hypothesis: principled forgetting is Pareto-dominant on long-horizon tasks. Cognitive forgetting / efficiency lens.
- **W8 — Right to Be Forgotten.** Workload that stress-tests the deletion machinery introduced in v0.1.1 — heavy use of inside-session deletion, cross-session propagation, deletion-during-supersession, and adversarial introduce-then-delete patterns. Regulatory / privacy lens. The v0.1.1 data model is the substrate; W8 is the dedicated workload.

(W7 and W8 are distinct mechanisms addressing distinct research questions and ship together in v0.2.)

---

## 9. Next deliverables

1. `02-backend-interface.md` — `MemoryBackend` Protocol with `write` / `supersede` / `delete` / `retrieve` / `audit` methods and capability flags.
2. `03-eval-metrics.md` — mathematical definitions of M1–M7, including the W6 capability map and the deletion-leakage check.
3. `src/aml/generator/trace.py` — Python dataclasses + serialization with schema v0.1.1.
4. `src/aml/generator/workloads/w1.py` — first workload implementation.
5. Smoke benchmark green.
6. Checkpoint commit; proceed to W2.

---

*End of document.*
