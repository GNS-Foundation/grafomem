# GRAFOMEM

[![GMP Certified](https://img.shields.io/badge/GMP-Certified-00c853?style=flat-square&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBvbHlnb24gcG9pbnRzPSIxMiwyIDIxLDcgMjEsMTcgMTIsMjIgMywxNyAzLDciIGZpbGw9Im5vbmUiIHN0cm9rZT0iIzAwOTljYyIgc3Ryb2tlLXdpZHRoPSIyIi8+PGNpcmNsZSBjeD0iMTIiIGN5PSIxMiIgcj0iMyIgZmlsbD0iIzAwYzg1MyIvPjwvc3ZnPg==)](https://grafomem.com/registry.html)
[![GMP v0.2.0](https://img.shields.io/badge/GMP-v0.2.0-0099cc?style=flat-square)](https://grafomem.com/spec.html)
[![M8 1.000](https://img.shields.io/badge/M8-1.000-00c853?style=flat-square)](https://grafomem.com/registry.html)

**An agent-memory benchmark that became a memory protocol.**

GRAFOMEM began as a benchmark for one question — *what should a standard for agent
memory actually specify?* — and turned into the answer: a benchmark, a protocol
(**GMP**), an executable conformance suite, and a certified, persistent reference
implementation. The thesis in one line:

> Memory capabilities are **orthogonal**, a **declared** capability is not the same as
> **observed** behavior, and the only way to tell them apart is to **test** — so agent
> memory should be specified and conformance-checked like any other protocol.

Clean-room research project. [grafomem.com](https://grafomem.com)

---

## The thesis, in three results

1. **Four orthogonal axes.** A memory standard must separately specify: representational
   capability (versioning / supersession), embedding quality, retention policy, and a
   two-sided privacy primitive (deletion **and** tenant isolation). The benchmark shows
   these are separately specifiable and verifiable.
2. **Claims ≠ behavior.** A backend can *declare* `HARD_DELETE` or `MULTI_TENANT` and
   still leak forbidden data (findings F10, F12). The declaration is not the guarantee.
   This is the load-bearing result — and the reason a conformance suite has to exist.
3. **Protocol + conformance.** "Supports capability X" is defined operationally:
   *passes the conformance suite for X.* The spec, the suite, and implementations that
   certify against it all exist and agree.

---

## The stack

| Layer | What it is | Where |
|---|---|---|
| **Benchmark** | 10 workloads (W1–W10), 20 findings; locked corpus 135 traces / 61,754 turns / 17,612 queries (v0.2.0) | `src/aml/generator/`, `scripts/run_w*.py` |
| **Paper** | arXiv technical report | `docs/grafomem-paper.pdf` |
| **Spec** | GMP v0.2 (draft) — protocol semantics (RFC 2119) | `docs/gmp-spec-v0.2.md` |
| **Conformance** | executable §8: `supports X` ≝ passes the suite for X | `src/aml/eval/conformance.py` |
| **Reference** | in-memory backend, self-certifying | `src/aml/backends/gmp_reference.py` |
| **Wire** | HTTP + JSON binding; the client *is* a `MemoryBackend` | `src/aml/wire.py` |
| **Store** | persistent SQLite + sqlite-vec; survives restart | `src/aml/backends/sqlite_gmp.py` |

Each layer certifies the one beside it. The reference backend runs the conformance
suite **on itself**; the wire client runs the *same* suite **over a socket**; the
SQLite store runs it **on a file**. The contract is transport- and
implementation-independent by construction — not by assertion.

**v0.2:** W7–W10 built (findings F14–F20); W7, W9, W10 corpus-locked into v0.2.0, W8 held out; W10 (operational concurrency & isolation) gated by M8 isolation conformance — §4.10, gmp-spec §10.

---

## Key findings

The benchmark is the evidence base. Full table in the paper (Appendix C); the
load-bearing ones:

- **Capabilities are inert without the workload that needs them.** On a pure-vector
  retrieval task, declaring `SUPERSESSION_CHAIN` / `BI_TEMPORAL` changes nothing
  (Δ = +0.000). On a drift task, supersession recovers recall from 0.281 → 0.867 at a
  tight budget (**+0.585**). The capability matters exactly when the workload exercises it.
- **The embedder is the lever, not the capabilities.** Swapping the stub for a real
  embedder moves recall +0.510 at budget 32; toggling capabilities on the same task
  moves it +0.000.
- **Declared ≠ honest (F10, F12).** A backend that claims `HARD_DELETE` but soft-deletes
  leaks deleted facts with probability 1.0; one that claims `MULTI_TENANT` but shares an
  index leaks across tenants with probability 1.0. Both *pass their own type signature*
  and *fail conformance*. Deletion and tenant isolation unify at the read path — a single
  `Forbidden(q)` set — which is why one two-sided test catches both.

---

## Latency — reference store, locked corpus

The W1–W6 subset ingested into one growing SQLite + sqlite-vec store (N = 38,882
rows), BGE-small embeddings, on an Apple-Silicon laptop. Numbers are post-v0.2 (the
metadata-column pre-filter):

| op | count | p50 | p95 |
|---|---:|---:|---:|
| write | 37,015 | 10.17ms | 13.66ms |
| supersede | 2,262 | 9.85ms | 12.90ms |
| delete | 395 | **0.03ms** | 0.05ms |
| retrieve | 15,342 | 11.27ms | **27.80ms** |

What the numbers say:

- **The embedder is the floor.** Every op that embeds sits at ~10ms; `delete` (the one
  op that doesn't) is 0.03ms. The store's own machinery is sub-millisecond. Single-item
  write throughput is ~97/s on MPS — and a `write_many` bulk-ingest path that batches the
  embedder under one transaction hits **847/s (8.6×)** with an identical resulting store,
  confirming the embedder, not the store, was the entire write cost.
- **The v0.2 pre-filter crushed the tail.** In v0.1, selective queries (`as_of`, tenant)
  ranked-then-filtered and triggered an adaptive widening loop, putting retrieve p95 at
  **82ms**. v0.2 pushes the tenant/valid-time predicate into the KNN as metadata columns
  and bounds `k` by the char budget — p95 fell to **27.80ms** (−66%) with identical
  results, and the high-N p50 sits *at or below* v0.1's flat region.
- **Retrieve p50 still grows with N** (~10ms under 10k → ~25ms at 25–50k). sqlite-vec is
  brute-force — there is no ANN index — so the scan is O(N). The pre-filter made
  retrieval *correct and tight*, not *sublinear*; the next lever for retrieve-at-scale is
  an actual ANN index, not more tuning. At 38k rows / 25ms p50 it isn't needed yet.
- **sqlite-vec ≈ numpy brute force** on pure-vector workloads (every bucket within ~1ms).
  The store's value at this scale is **persistence and not pinning the corpus in RAM**,
  not speed — confirming the in-memory reference is the right default below large scale.

---

## Running it

Editable install (src-layout; this also puts `aml` on the path permanently):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[backends]"          # aml + sentence-transformers + torch
pip install sqlite-vec apsw           # for the persistent store
```

> **macOS note:** the store needs SQLite extension loading. If your Python's `sqlite3`
> lacks it, the backend transparently falls back to `apsw` (bundled SQLite). Keep `apsw`
> installed.

**Self-validating smokes** — each stands up a backend and runs the conformance suite
against it:

```bash
python -m aml.backends.gmp_reference   # reference impl certifies itself
python -m aml.wire                     # conformance suite passes over HTTP
python -m aml.backends.sqlite_gmp      # persistent store: survives reopen + passes suite
```

**Benchmark experiments** and the **scale probe**:

```bash
python -m scripts.run_w1               # F1, F2 — vector vs recency floor + budget sweep
python -m scripts.run_w2               # F3–F5 — drift, supersession, bi-temporal
python -m scripts.run_w3               # F6, F7 — distractor noise; the embedder lever
python -m scripts.scale_probe          # corpus latency + sqlite-vec vs brute force
```

---

## Status & roadmap

**v0.1 — complete.** Benchmark, paper, GMP v0.1 spec, conformance suite, in-memory
reference (certified), HTTP+JSON wire binding (suite passes over a socket), persistent
SQLite + sqlite-vec store (survives restart, passes the full profile), scale probe.
v0.1 normative subset: `{AUDIT, SUPERSESSION_CHAIN, BI_TEMPORAL, HARD_DELETE, MULTI_TENANT}`.

**v0.2 — in progress.**

- **Metadata-column pre-filter** in the store — **done.** `tenant_id` / valid-time live
  in the vec0 table (nulls sentinel-encoded, since sqlite-vec metadata filters don't
  support `IS NULL`); the KNN filters natively and `k` is bounded by the char budget.
  Drops retrieve p95 82→28ms with identical results and exact selective-filter retrieval;
  the O(N) brute-force scan remains (no ANN index — a separate lever, not needed at this
  scale).
- **Batched embedding** on the ingest path — **done.** A `write_many` fast-path embeds a
  batch in one forward pass under one transaction: **97 → 847 items/s (8.6×)**, same
  resulting store. Optional accelerator; the single-`write` Protocol path is unchanged.
- **Reserved capabilities — provenance pair done.** `PROVENANCE` (normative) and
  `CRYPTOGRAPHIC_PROVENANCE` (optional) are implemented in both backends and certified by the
  v0.2 conformance suite: Ed25519 over a content-store fact_id, `source` persisted (signatures
  survive restart). Provenance has **no benchmark workload by design** — it is integrity metadata,
  "verifiability, not ranking" (gmp-spec §7.5), so the suite tests it with constructed probes like
  `AUDIT`, not a retrieval workload (§8.3).
- **Workloads W7–W9 — built.** W7 (Conflict Detection), W8 (Forgetting Curve), W9 (Cross-Session
  Deletion); generators, backend spectrums, runners, findings F14–F18. W7 and W9 are corpus-locked
  into v0.2.0; W8 is held out pending the summarise/merge retention variant. These home the last
  two reserved flags — `CONFLICT_DETECTION` (W7) and `CROSS_SESSION_PROPAGATION` (W9), now
  **un-reserved** in gmp-spec §7.4.
- **W10 — Operational Concurrency & Isolation — built and corpus-locked.** Trace-schema v0.2
  carries set-valued ground truth; `interface.py` adds `submit_concurrent` (gated by
  `CONCURRENCY_CONTROL`, the 10th flag), `IsolationPolicy`, and a `declared_policy`
  self-description on the concurrent backend. A five-store spectrum (serializable →
  resurrecting) drives **M8 isolation conformance**, which catches both the over-claimer
  (declares serializable, delivers read-committed — F19) and the §10.4 durability violator
  (resurrects a committed delete — F20). Locked into v0.2.0 (135 traces). The suite's last
  open frontier, now closed.

The arc is protocol-first: the spec and suite are the standard; the implementations are
the proof it's real and runnable. "Postgres for agent memory" is the destination, not
the starting point.
