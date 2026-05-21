# W5 — Tenant Isolation: the second boundary of the privacy primitive

**Findings F12–F13.** Real BGE (`BAAI/bge-small-en-v1.5`), hard, budget 512, seeds 0–4.
Generator: `generate_w5` (deterministic, R1). Runner: `scripts/run_w5.py`.

## Result

Every subject exists under every tenant with a different object, so each tenant's
query is byte-identical to another tenant's (e.g. both ask "Where is Kesia
located?", but tenant-a's answer is *Marrowfen* and tenant-b's is *Highford*).
Two axes per query: **leakage** (M7 — a returned fact owned by a *different*
tenant is a privacy violation) and **in-tenant recall** (the tenant's own fact
must still be retrievable). Four backends share one BGE store:

| backend         | leakage rate | in-tenant recall | verdict        |
|-----------------|:------------:|:----------------:|----------------|
| vector_only     | 1.000        | 1.000            | LEAKS          |
| leaky_tenant    | 1.000        | 1.000            | LEAKS          |
| tenant_scoped   | 0.000        | 1.000            | PASS (both)    |
| over_isolating  | 0.000        | 0.000            | OVER-ISOLATES  |

- leakage, leaky − scoped: **+1.000**, 95% CI [+1.000, +1.000]
- in-tenant recall, scoped − over: **+1.000**, 95% CI [+1.000, +1.000]

Only `tenant_scoped` is correct on both axes.

## F12 — A capability claim does not certify isolation (leakage)

`leaky_tenant` claims `MULTI_TENANT`, accepts a `tenant_id` on every write and
retrieve, tags each fact with its owner — and then leaks every other tenant's
fact, *identically* to `vector_only`, which makes no such claim. The API surface
is impeccable (capability advertised, every call accepts the tenant); the
isolation contract is simply not enforced on read. Nothing in the interface
catches this — only the leakage check does. It is the exact tenancy analog of
W6's `soft_delete`: **the advertised capability is not the enforced behavior**,
and a benchmark/conformance suite is the only thing that separates the claim from
the truth.

Note the clean orthogonality on a real embedder: the two leaky backends leak at
1.000 while holding in-tenant recall at 1.000. Leakage and recall are independent
— a store can be perfectly useful to its owner *and* perfectly porous to its
neighbours at the same time. Privacy failure leaves no recall footprint to warn
you.

## F13 — Over-isolation: the false-negative direction

`over_isolating` scopes correctly to the querying tenant but adds an
over-cautious heuristic: any subject that appears under more than one tenant is
withheld ("ambiguous — might leak, so don't return it"). It leaks nothing
(0.000), yet because W5 shares every subject across tenants, it withholds
*everything* and in-tenant recall collapses to 0.000 — it refuses to answer its
own tenant's legitimate queries in the name of safety. Isolation done too
aggressively is as broken as isolation not done at all; the two are independent
failure directions, not a single trade-off. Correct isolation is **exact**: every
fact visible to its owner, none to anyone else. Only `tenant_scoped` achieves it.

## Mechanism

- Leakage is structural: a leaked fact is one whose owning tenant differs from
  the querying tenant yet still ranks into the returned set. The shared-subject
  construction makes the cross-tenant fact a top-ranked candidate, so a
  tenant-blind store leaks on every query.
- The result is **embedder-invariant** on the isolation axis: which records a
  query may see is decided by the backend, not the embedder, so the stub
  reproduces the leakage/over-isolation columns exactly. (The only embedder
  sensitivity is a budget-edge tie-break that nudged the leaky pair's recall to
  0.998 under the stub; real BGE ranks the own fact cleanly into budget at
  1.000.)
- `over_isolating`'s collapse to 0.000 is the extreme case (every subject is
  shared, so the "ambiguous" filter hides all of them). The finding is the
  *direction*, not the magnitude.

## Synthesis — the privacy primitive, closed

W5 and W6 are a matched pair: the **fourth axis** (the privacy/safety axis from
the W6 synthesis) has two boundaries, and each behaves the same way.

> | boundary | leakage (FP) | over-restriction (FN) | claims-but-leaks |
> |---|---|---|---|
> | **deletion** (W6) | forgotten fact resurfaces | survivors purged | `soft_delete` (F10) |
> | **tenant** (W5) | other tenant's fact returned | own facts withheld | `leaky_tenant` (F12) |

Both boundaries are **two-sided** (a store can leak, over-restrict, or both),
**embedder-invariant**, and **not certified by a capability claim** — each has a
backend that advertises the right flag and fails anyway. A memory protocol must
therefore specify *isolation and deletion semantics*, and a conformance suite
must test *both directions of both boundaries*. Capability flags are necessary
but never sufficient.

## Reproduce

```
python scripts/run_w5.py        # real BGE; ~seconds after model load
```

Traces: `generate_w5`, seeds 0–4, deterministic (R1). Locked in
`grafomem-bench-v0.1.8`; W5 workload rollup
`a791dae52e917340708da237205e005c7abaa1f5d52c0661cc20fa864dc4d010`. Adding W5
leaves the W1–W4 and W6 rollups byte-identical (verified on regeneration).
