# W6 — Deletion / Leakage: the two-sided forget primitive

**Findings F10–F11.** Real BGE (`BAAI/bge-small-en-v1.5`), hard, budget 512, seeds 0–4.
Generator: `generate_w6` (deterministic, R1). Runner: `scripts/run_w6.py`.

## Result

Each subject is given several facts; one fact per subject is deleted, the rest
survive. Two probes per subject: a **deleted-probe** (its answer must be empty —
a leak is a privacy violation) and a **survivor-probe** (its answer must still be
retrievable — dropping it is over-deletion). Four backends, all sharing one BGE
store:

| backend        | leakage rate | survivor recall | verdict       |
|----------------|:------------:|:---------------:|---------------|
| vector_only    | 1.000        | 1.000           | LEAKS         |
| soft_delete    | 1.000        | 1.000           | LEAKS         |
| honest_delete  | 0.000        | 1.000           | PASS (both)   |
| coarse_delete  | 0.000        | 0.000           | OVER-DELETES  |

- leakage, soft − honest: **+1.000**, 95% CI [+1.000, +1.000]
- survivor, honest − coarse: **+1.000**, 95% CI [+1.000, +1.000]

Only `honest_delete` is correct on both axes.

## F10 — A capability claim does not certify deletion (leakage)

`soft_delete` claims `HARD_DELETE`, accepts `delete()`, returns `True`, and then
leaks every deleted fact — *identically* to `vector_only`, which makes no such
claim. The type contract (capability advertised, call succeeds) is satisfied; the
semantic contract is not. Worse, `soft_delete`'s `audit()` reports the fact as
gone while `retrieve()` still surfaces it: the audit log and the retrieval path
disagree about what was forgotten. Nothing in the interface catches this — only
the leakage check does. This is the concrete argument for a conformance suite: a
backend's *advertised* capabilities cannot be trusted, so deletion must be
*tested* against ground truth, not assumed from a flag.

## F11 — Forgetting is two-sided (over-deletion)

`coarse_delete` deletes by subject rather than by fact: deleting one of a
subject's facts purges all of them. It leaks nothing (leakage 0.000) yet drives
survivor recall to 0.000 — it forgets far more than it was asked to. Leakage and
over-deletion are **independent failure directions**: a backend can remember too
much, forget too much, or both. These are not two ends of one trade-off; they are
two separate ways to be wrong, and a single-sided metric would miss whichever one
it didn't measure. Correct deletion is *exact*: the deleted fact gone, every other
fact untouched. Only `honest_delete` achieves it.

## Mechanism

- Leakage is structural, not statistical: a leaked fact is simply one still
  present and retrievable in the store. The deleted-probes (content matching the
  deleted fact) make the leak maximally tempting, but `vector_only` and
  `soft_delete` leak because the deleted fact remains a top-ranked candidate at
  query time.
- The result is **embedder-invariant**. Real BGE reproduces the stub-embedder
  table to three decimals. Unlike W3 — where the embedder *is* the lever —
  deletion correctness has nothing to do with embedding quality; it is a property
  of the store's write/delete/retrieve semantics, like the F9 retention cliff.
- `coarse_delete`'s collapse to 0.000 is the extreme case (every subject carries
  a deletion, so a subject-level purge empties the store). The finding is the
  *direction*, not the magnitude.

## Synthesis — a fourth axis

W1–W4 established three orthogonal levers: representational **capability**
(W1/W2 — supersession, bi-temporal), **embedding quality** (W3), and **retention
policy** (W4). W6 adds a fourth, orthogonal to all three:

> **Deletion correctness** — a two-sided consistency primitive (no leakage, no
> over-deletion), independent of the embedder (F10/F11 are embedder-invariant)
> and *not* certified by a capability claim (`soft_delete` advertises
> `HARD_DELETE` and fails anyway). This is the privacy/safety axis a memory
> protocol must specify and a conformance suite must enforce.

## Reproduce

```
python scripts/run_w6.py        # real BGE; ~seconds
```

Traces: `generate_w6`, seeds 0–4, difficulty hard, deterministic (R1). W6 corpus
inclusion pending (`grafomem-bench-v0.1.7`); the per-workload rollup hash will be
cited here once locked.
