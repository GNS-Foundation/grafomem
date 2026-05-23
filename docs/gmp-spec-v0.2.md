# GRAFOMEM Memory Protocol (GMP) — v0.2 (draft)

Camilo Ayerbe Posada · GNS Foundation · ULISSY s.r.l. · grafomem.com

> **Status:** draft, v0.2. This document specifies *semantics*. It is the
> follow-up promised in §6 and §8 of *GRAFOMEM: A Reproducible Benchmark for Agent
> Memory* (the "paper"): the paper establishes, empirically, the dimensions a
> memory standard cannot leave unspecified (requirements R1–R5 and findings
> F1–F13); this document commits to a specification of each. v0.1 specified the
> five core capabilities (§3–§6); v0.2 adds provenance (§7.5). Every normative
> section cites the requirement and the findings it rests on, and Appendix A is the
> full traceability matrix. Working name; rename at will.

---

## 0. Scope and conventions

**Scope (D1).** GMP v0.1 specifies the *abstract operational semantics* of a memory
store and the *conformance contract* a store must satisfy to claim a capability. It
is transport-agnostic: it defines operations, types, and guarantees, not a wire
encoding. The Python `MemoryBackend` Protocol (`interface.py`, v0.1.1) is the
**reference binding** (Appendix B). A concrete wire binding (gRPC/HTTP/JSON) is a
separate document, deliberately deferred.

**Normative language.** The key words MUST, MUST NOT, SHOULD, SHOULD NOT, and MAY
are to be interpreted as in RFC 2119.

**Why a protocol, not a library.** The paper's decisive result (§5.2; F10, F12) is
that a capability *claim* does not certify *behavior*: a store can advertise the
correct flag, satisfy the type contract, accept every call, report success, and
still leak completely. A library's type signature cannot express the guarantees in
§3–§6; only an external, oracle-grounded conformance suite (§8) can. GMP therefore
specifies behavior and makes the conformance suite part of the protocol.

---

## 1. Model

### 1.1 Facts

The atom of the model is a **fact**: a quadruple

```
(predicate, subject, object, valid_from)
```

carrying additional fields — a monotonic `sequence`, an `importance` weight, an
optional `valid_until` and `superseded_by` (versioning, §3), and a `tenant_id`
(tenancy, §6). The `content` of a write is the text rendering of a fact; the
quadruple is its canonical form. A store that accepts only opaque text is the
degenerate case in which `object` is the content and `predicate`/`subject` are
null; structured facts are RECOMMENDED because identity, supersession, and
deduplication (below) are defined over the quadruple.

### 1.2 Identity — tenant-scoped (D2; resolves the paper's open question)

A fact's identity is content-derived and **tenant-scoped**:

```
fact_id = BLAKE2b-128(tenant_id ‖ predicate ‖ subject ‖ object ‖ valid_from)
```

This resolves the question §6 of the paper deliberately left open. The benchmark's
oracle uses a content-only identity that *excludes* `tenant_id`, sidestepping
collision by construction; GMP instead **includes** `tenant_id` in identity. The
consequences are intended:

- Two tenants asserting the same `(P, S, O, valid_from)` hold **distinct** facts.
  Cross-tenant deduplication is therefore impossible — which is the correct
  property to lose, because it is exactly the coupling that lets an isolation bug
  become a data-identity bug.
- Within a tenant, identity excludes `sequence`, so re-introducing the same fact is
  idempotent in identity.
- Deletion (§6.2) and supersession (§3.1) key on `fact_id` and thus cannot reach
  across tenants: isolation and deletion compose cleanly (§6.5).

The single-tenant deployment is the special case `tenant_id = ` a reserved default
namespace. A store that does not declare `MULTI_TENANT` operates wholly within that
default namespace.

### 1.3 References

A `MemoryRef` is an **opaque** token returned by `write` and `supersede`. Its only
defined operation is equality; a store MAY use any concrete representation (UUID,
content hash, path). The protocol never inspects a ref's structure.

### 1.4 The store

A store holds a set of fact-versions, exposes the operations of §2, and honors the
guarantees of §3–§6 **for the capabilities it declares** (§7). It declares its
capabilities once (§7.2); an operation requiring an undeclared capability MUST raise
`CapabilityNotSupported`.

---

## 2. Operations

Seven operations. `capabilities`, `write`, `retrieve`, and `flush` are always
required; the rest are gated on a declared capability.

| operation | requires | summary |
|---|---|---|
| `capabilities() → set` | — | stable capability set; read once (§7.2) |
| `write(content, opts) → ref` | — | persist a new fact-version; return its ref |
| `supersede(old_ref, content, opts) → ref` | `SUPERSESSION_CHAIN` | retire `old_ref` from the answer set; return the successor (§3.1) |
| `retrieve(query, opts) → [Memory]` | — | ranked facts within budget (§4); `as_of`→`BI_TEMPORAL`, `tenant_id`→`MULTI_TENANT` |
| `delete(ref) → bool` | `HARD_DELETE` | hard-delete; unrecoverable on the read path (§6.2); idempotent |
| `audit() → iter[Memory]` | `AUDIT` | all retrievable facts incl. superseded, **excl.** hard-deleted |
| `flush() → none` | — | barrier: block until prior mutations are durable and visible |

**`write`.** MUST persist the fact and return a ref. If `opts.signing_key` is set
the store MUST sign per §7.4 and MUST declare `CRYPTOGRAPHIC_PROVENANCE`.

**`retrieve`.** MUST return facts relevant to `query` within `opts.budget` (§4).
`as_of` without `BI_TEMPORAL`, or `tenant_id` without `MULTI_TENANT`, MUST raise
`CapabilityNotSupported`. MUST be deterministic for fixed state (§4.3).

**`delete`.** Post-condition: the fact is unrecoverable via `retrieve()` **and**
`audit()` (§6.2). Returns `false` if the ref is unknown; MUST NOT raise on an
already-deleted ref.

**`audit`.** Yields every retrievable fact including superseded versions (with
`superseded_by` linked), and MUST exclude hard-deleted facts. `audit` is the
read-path surface on which the deletion guarantee (§6.2) is also checked.

---

## 3. Versioning and validity (R1 ← F3–F5; D3)

A protocol that treats memory as an append-and-retrieve log cannot express the most
common real-world event — a fact changing — and returns stale data alongside
current data with no signal to separate them (the "budget illusion," F3). GMP makes
versioning first-class.

### 3.1 Supersession — MUST if `SUPERSESSION_CHAIN`

`supersede(old_ref, content, opts)` writes a successor and **retires** the
predecessor from the answer set: a current query's candidate set MUST exclude
superseded facts, so the answer set contains only current heads (one per
`(subject, predicate)` chain). This is the operation that recovers tight-budget
recall under drift (F4: 0.281 → 0.867, +0.585, embedder held constant).

- If `old_ref` is unknown, `supersede` MUST behave as a fresh `write`.
- `audit()` MUST still yield the superseded version, with `superseded_by` set to the
  successor's ref.
- Supersession is a **representational** commitment, independent of retrieval
  quality (§4.4).

### 3.2 Valid time — MUST if `BI_TEMPORAL`

A store declaring `BI_TEMPORAL` MUST maintain a valid-time interval
`[valid_from, valid_until)` per version and resolve `retrieve(as_of=t)` to the
versions valid at `t`:

```
valid_at(f, t)  ≡  f.valid_from ≤ t < f.valid_until        (absent valid_until = +∞)
```

`supersede(old, new)` MUST close the predecessor's interval at the successor's
`valid_from`, reconstructing contiguous windows. `as_of = None` resolves to the
open-interval heads — identical to §3.1. This is the only way historical questions
become answerable at all (F5: ~375 historical queries/seed move from N/A to ~1.0).
`BI_TEMPORAL` implies `SUPERSESSION_CHAIN` (§7.3).

### 3.3 Relationship to the active predicate

§3.1–§3.2 are the store-side realization of the paper's active-memory predicate
(paper §3.3): a fact is answerable for a query only if it was introduced before the
query in transaction time **and** holds at the query's valid time **and** has not
been deleted (§6) **and** belongs to the querying tenant (§6). Versioning specifies
the first two conjuncts; privacy (§6) specifies the last two.

---

## 4. Retrieval and the budget contract (R2 ← F6–F7; D4)

Embedding quality is a genuine lever — it is the *only* thing that moves
discrimination under distractor noise (F7: +0.510) — but it is orthogonal to every
other axis and improves on the cadence of model releases, not protocol revisions.
GMP specifies the retrieval *interface* and a *budget contract*, and deliberately
does **not** mandate an embedder.

### 4.1 Interface

`retrieve(query, opts)` takes a query and options `{budget, as_of, tenant_id,
top_k}` and returns a ranked list of facts. `top_k` is a non-contractual hint;
`budget` is contractual.

### 4.2 Budget

`budget` is expressed in **tokens**. A store MUST declare its tokenizer; for
conformance the normative proxy is total character count (the paper's
deterministic, embedder-independent stand-in). A store MUST NOT return facts whose
total cost exceeds `budget`, and MUST rank — returning the facts it judges most
relevant within budget and dropping the tail. A capable store wins at a tight budget
and ties at a saturating one (F2, F4); the budget contract is what makes that
visible.

### 4.3 Determinism

For fixed store state, a fixed `query`, and fixed `opts`, `retrieve` MUST be
deterministic: the same set **and** the same order. Determinism is required for the
conformance suite to be oracle-grounded and reproducible (paper §3.5).

### 4.4 Embedder-agnosticism

The protocol specifies **what** must be rankable (the active facts of §3.3, within
the privacy boundaries of §6) and the budget; it does **not** specify **how** they
are ranked. Ranking quality is out of scope: a conformant store MAY use any ranking
function. Crucially, the guarantees of §5 and §6 MUST hold **under any embedder** —
they are properties of which facts the store *admits as candidates*
(`Visible(q)`), not of how candidates are ordered (paper Proposition 2). Conformance
(§8) tests the contract — active set, budget, determinism, and two-sided privacy —
never ranking quality. Fixing an embedder into the standard would couple an axis the
evidence shows is independent (F7: capabilities move recall under noise by +0.000)
and date the standard to a model generation.

---

## 5. Retention (R3 ← F8–F9; D5)

Every store makes a retention choice, and that choice imposes a hard, structural
limit on the answerable horizon at a footprint cost the consumer must be able to
reason about. GMP makes retention a declared, first-class axis.

### 5.1 Declared policy

A store MUST declare a `RetentionPolicy`:

```
RetentionPolicy = {
  kind: unbounded | bounded_count(K) | bounded_time(T) | compacting(rule),
  params,
  coverage_guarantee
}
```

### 5.2 Coverage contract

`coverage_guarantee` is a predicate, readable by an agent, stating the conditions
under which a fact remains retrievable. For `bounded_count(K)` it is "a fact remains
retrievable while fewer than `K` facts have been written since it" — i.e. the
forgetting cliff at dependency depth `d = K`, which is structural and
embedder-invariant (paper Proposition 1; F9 confirms it at exactly `K = 64`). An
agent depending on a long-ago fact MUST be able to read the coverage guarantee and
determine whether the store can still be expected to hold it.

### 5.3 Footprint

A store SHOULD expose its footprint contract — store size and per-query scan cost as
a function of horizon. `unbounded` grows linearly at flat recall 1.000;
`bounded_count(K)` plateaus at `K` while overall recall declines as the horizon
exceeds the window (F8: 0.659 → 0.348). The footprint/coverage pair is the actual
tradeoff a consumer is choosing between.

### 5.4 Retention is not deletion

Eviction under a retention policy is capacity management, not `HARD_DELETE`: the
caller did not request removal. Evicted facts MUST be reflected in `audit()` only
insofar as they remain in the retained window — i.e. `audit()` exposes exactly the
retained window. The deletion guarantee (§6) and the retention contract (§5) are
distinct boundaries and MUST NOT be conflated.

---

## 6. Two-sided read-path privacy (R4 ← F10–F13; D6)

The safety-critical core. Two boundaries — deletion and tenant isolation — are
structurally identical, and each fails in two directions: **leakage** (returning
what must not be returned) and **over-restriction** (withholding what must be). GMP
specifies both boundaries, two-sided, and locates the guarantee on the read path.

### 6.1 Granted and forbidden sets

For a query `q` issued in tenant `T` at transaction time `t`:

- `Granted(q) = G(q)` — the active facts the query requires: within tenant `T`,
  valid at the query's valid time, introduced by `t`, and not deleted (paper §3.3).
- `Forbidden(q)` — the facts deleted at or before `t`, together with all facts owned
  by a tenant other than `T` (paper §3.6).

Let `R(q)` be the set a store returns. The two failure directions are:

```
leakage(q)            ≡  R(q) ∩ Forbidden(q) ≠ ∅          (false positive)
over-restriction(q)   ≡  G(q) ⊄ R(q)                       (false negative)
```

### 6.2 The deletion guarantee — `HARD_DELETE`

After `delete(ref)` returns, for every subsequent query the deleted fact MUST NOT
appear in `retrieve()` **or** `audit()` (no leakage), **and** deletion MUST NOT
remove any non-deleted fact (no over-deletion). The two-sided PASS condition is:

```
leakage over deleted facts = 0      AND      survivor recall = 1
```

A tombstone that leaves the fact retrievable (`soft_delete`) FAILS the leakage
direction (F10); a delete that removes more than the target (`coarse_delete`) FAILS
the over-restriction direction (F11); only honest deletion (`honest_delete`) passes
both.

### 6.3 The tenant guarantee — `MULTI_TENANT`

`retrieve(tenant_id = T)` MUST return only facts owned by `T` (no cross-tenant
leakage) **and** all of `T`'s active required facts (no over-restriction). The
two-sided PASS condition is:

```
cross-tenant leakage = 0      AND      in-tenant recall = 1
```

A store that accepts the tenant tag but ignores it on retrieval (`leaky_tenant`)
FAILS the leakage direction (F12); a store that over-scopes and withholds in-tenant
facts (`over_isolating`) FAILS the over-restriction direction (F13); only
`tenant_scoped` passes both. The paper establishes that leakage is orthogonal to
recall — a store can leak at 1.000 *while holding recall at 1.000* — so recall alone
can never detect the failure.

### 6.4 The guarantee is on the read path (decisive)

The guarantee is a property of what `retrieve()` and `audit()` **return**, not of
whether the store **accepted** the write-side call. A store that accepts a tombstone
or a tenant tag, reports success, and then ignores it on retrieval is
**non-conformant**, regardless of the success it reported. This is the direct
specification of the paper's claim≠behavior result (§5.2): the flag, the type
contract, and the accepted call are all satisfied by the leaking backends.

### 6.5 Identity interaction (D2)

Because identity is tenant-scoped (§1.2), `delete` is keyed on `(tenant, fact_id)`
and cannot reach a different tenant's identical-content fact, and a tenant scope is
a partition of identity space rather than a filter layered over a shared one.
Isolation and deletion therefore compose without a cross-boundary hazard — the
concrete payoff of resolving the open question by tenant-scoping rather than by
content-only identity.

---

## 7. Capabilities and declaration

### 7.1 The capability set

Ten flags, enumerated and append-only across versions (`interface.py`):

```
BI_TEMPORAL   HARD_DELETE   SUPERSESSION_CHAIN   CROSS_SESSION_PROPAGATION
MULTI_TENANT  CONFLICT_DETECTION   PROVENANCE   CRYPTOGRAPHIC_PROVENANCE   AUDIT
CONCURRENCY_CONTROL
```

### 7.2 Declaration discipline

`capabilities()` MUST return a stable set, read once at setup. An operation
requiring a capability the store does not declare MUST raise
`CapabilityNotSupported`. Honest omission MUST NOT be penalized by the conformance
suite — a store's declared type is an honest statement of what it will attempt, and
the suite adapts to the declaration (paper §3.7, anchor B1).

### 7.3 Dependencies

- `BI_TEMPORAL` implies `SUPERSESSION_CHAIN` (valid time presupposes the retire
  operation; the reference `bi_temporal` backend declares both).
- `CRYPTOGRAPHIC_PROVENANCE` implies `PROVENANCE`.
- `AUDIT` is the floor for `audit()`.

### 7.4 Normative subsets

GMP v0.1 fully specifies `SUPERSESSION_CHAIN`, `BI_TEMPORAL`, `HARD_DELETE`,
`MULTI_TENANT`, and `AUDIT` (§3–§6). GMP v0.2 adds `PROVENANCE` to the normative
subset and `CRYPTOGRAPHIC_PROVENANCE` as an optional extension — a store MAY decline
to sign, but if it claims the flag it MUST honor §7.5. `CONFLICT_DETECTION` and
`CROSS_SESSION_PROPAGATION` are **no longer reserved** — v0.2 specifies and homes them
(W7 §4.7, W9 §4.9), and a store MAY claim them under conformance. `CONCURRENCY_CONTROL`
is the sole remaining **reserved-but-being-specified** flag: §10 defines it, and it
enters the normative subset when §10 is ratified. Until then a store MUST NOT claim it
under conformance.

### 7.5 Provenance — `PROVENANCE` (v0.2 normative) and `CRYPTOGRAPHIC_PROVENANCE` (v0.2 optional)

Provenance attaches integrity metadata to a memory without changing *which* memories
retrieve: it is verifiability, not ranking. Both flags are independent of the
embedder and of the retrieval result, so Proposition 2 (swap the embedder, the
guarantees are unchanged) holds for them as it does for the safety capabilities.

**`PROVENANCE` — MUST if claimed.** Every memory returned by `retrieve` or `audit`
MUST carry a non-null `source` (`SourceMeta`) with at least `write_id` — a stable
identifier for the write event, for which the opaque ref is a sufficient choice — and
`written_at`, the store's record of the write time. A store that declares
`PROVENANCE` and returns `source = None`, or omits `write_id`, is in violation.
Provenance MUST survive the read path and any persistence boundary (process restart).

**`CRYPTOGRAPHIC_PROVENANCE` — MUST if claimed; implies `PROVENANCE` (§7.3).** When
`WriteOptions.signing_key` is set, the store MUST sign the memory's `fact_id` with
that Ed25519 key and populate `source.signature` (64 bytes) and `source.public_key`
(32 bytes); `written_by` SHOULD record the public key. Verification is the canonical
`verify_provenance(memory, expected_fact_id)` (Appendix B): a memory verifies iff its
signature is valid over `expected_fact_id` under its public key. Because the signature
binds to the exact `fact_id`, post-write tampering is detectable — a verifier that
recomputes the `fact_id` from altered content gets `False`.

**The content-store `fact_id`.** §1.2 defines the *structured* `fact_id` over
`(tenant_id, predicate, subject, object, valid_from)` — the identity of a fact. A
*content store* (the GMP reference and SQLite backends) holds verbalized content, not
a (P, S, O) triple, and its `write(content, options)` never receives the triple; its
cryptographic commitment is therefore over the unit it actually stores:

```
fact_id = BLAKE2b-128(content ‖ sep ‖ tenant_id)        # content-store binding
```

with `sep` the unit separator and a null tenant encoded as the empty string.
`valid_from` is deliberately excluded: provenance MUST be verifiable from the
*retrieved* memory, and a store that keeps `valid_from` at reduced precision (an epoch
REAL) would not reproduce it byte-for-byte, whereas content and tenant round-trip
exactly. Versions are already distinguished by differing content, so `valid_from` is
not needed for identity here. This is the content-store binding of the §1.2
commitment; a structured-fact store signs the §1.2 `fact_id` directly. Either way the
signed object is the canonical identifier of the unit committed to.

---

## 8. Conformance (R5 ← §5.2)

The conformance suite is **part of the protocol**, not an optional appendage.

### 8.1 Definition of support

> A store **supports** capability `X` if and only if it **passes the conformance
> suite for `X`** — not if it *declares* `X`.

A store that declares `X` but fails the suite for `X` is **non-conformant**; the
mismatch is a `ConformanceViolation` (Appendix B). Conformance is the gate between
"type-checks" and "is safe."

### 8.2 The suite

The suite MUST be executable and oracle-grounded, and MUST test each claimed
capability **in both directions**, with the two safety capabilities tested
two-sided (leakage AND over-restriction). The GRAFOMEM benchmark's workloads W1–W6,
its oracle, and its metrics (M1 recall, M2 precision, M7 cross-tenant leakage, and
the deletion-leakage metric) are the normative **first draft** of the suite. Safety
directions MUST be reported with paired-bootstrap confidence intervals (paper §3.6);
a direction passes only when the interval excludes the failing outcome.

### 8.3 Per-capability obligations

| capability | conformance obligation | direction(s) | evidence |
|---|---|---|---|
| `AUDIT` | `audit()` yields all retrievable incl. superseded, excl. deleted | one | — |
| `SUPERSESSION_CHAIN` | current query returns heads only; `audit` yields history | one | F4 |
| `BI_TEMPORAL` | `as_of=t` resolves to the version valid at `t` | one | F5 |
| `HARD_DELETE` | no leak of deleted facts **and** no over-deletion of survivors | **two** | F10, F11 |
| `MULTI_TENANT` | no cross-tenant leak **and** no in-tenant over-restriction | **two** | F12, F13 |
| `PROVENANCE` | every written memory exposes `source` (`write_id` + `written_at`) | one | — |
| `CRYPTOGRAPHIC_PROVENANCE` | a signed write verifies **and** an altered-content `fact_id` does not | **two** | — |

The two provenance rows are *constructed* tests (like `AUDIT`): the suite writes its
own probes — unsigned for `PROVENANCE`, signed with a generated Ed25519 key for
`CRYPTOGRAPHIC_PROVENANCE` — rather than replaying a workload, since provenance is
integrity metadata and does not change retrieval. The two-sided crypto test gates
signature validity (`>= 1 - eps`) and tamper acceptance (`<= eps`).

A store claiming `CONCURRENCY_CONTROL` MUST pass the W10 isolation suite for its
**declared** level — every observed outcome must lie in the permissible set of that
level, and no `forbidden_outcome` (notably a resurrected committed delete, §10.4) may
occur. A claimed level stronger than the achieved level is a conformance failure,
reported as a downgrade (§10.5).

### 8.4 Reporting

A conformance run MUST report, per claimed capability, a pass/fail with the
oracle-derived metric(s) and — for the two-sided capabilities — the interval in each
direction. A store's conformance profile is the set of capabilities it *passes*, and
it is this profile, not its declaration, that a consumer relies on.

---

## 9. The open question, resolved

The paper (§6) surfaces one requirement it deliberately leaves open: whether memory
identity is content-only or tenant-scoped — a choice with direct consequences for
deduplication, isolation, and deletion. GMP v0.1 resolves it as **tenant-scoped**
(§1.2). The discarded alternative, content-only identity, buys cross-tenant
deduplication (a single physical fact shared by tenants asserting it) at the cost of
entangling identity with the isolation boundary: a store would have to enforce
isolation as a filter over shared identities, exactly the layering whose failure the
paper documents (F12). Tenant-scoping makes a tenant scope a *partition* of identity
rather than a *filter* over a shared space, so the isolation guarantee (§6.3) and the
deletion guarantee (§6.2) compose without a cross-boundary hazard (§6.5). The cost —
no cross-tenant dedup — is the correct thing to forgo for a safety boundary. This is
a v0.1 commitment and is revisitable should a deployment present a dedup requirement
that outweighs it.

---

## 10. Operational concurrency and isolation (R6 ← W10; D7) — `CONCURRENCY_CONTROL`

When more than one writer (or a writer and a reader) contend for the same
`(subject, predicate)` without an externally imposed order, the outcome depends on the
store's isolation level. GMP makes that level a declared, first-class property. This axis
is **distinct from §6 conflict semantics**: W7-style conflict is *logical* (two facts with
overlapping validity, both live, no defined current value — see §3, `CONFLICT_DETECTION`);
isolation is *operational* (concurrent operations whose relative order is not fixed).
`CONFLICT_DETECTION` and `CONCURRENCY_CONTROL` are independent capabilities.

A store that does not declare `CONCURRENCY_CONTROL` is single-order: every operation is
totally ordered (the v0.1 model), the axis does not apply, and the store is **skipped**
under the W10 suite — exactly as a non-`MULTI_TENANT` store is skipped under W5. Declaring
the capability makes ground truth **set-valued**: the permissible outcomes are those of
*some* serialization admitted by the declared level.

### 10.1 Declared policy

A store claiming `CONCURRENCY_CONTROL` MUST declare an `IsolationPolicy`:

```
IsolationPolicy = {
  level: read_committed | snapshot | serializable,
  conflict_rule: first_committer_wins | last_committer_wins | abort_both | merge,
  coverage_guarantee
}
```

`conflict_rule` MUST resolve **write–write** conflicts (two concurrent `supersede`s on one
key) **and write–delete** conflicts (a `delete` concurrent with a `supersede` of the same
key — delete-then-supersede targets an absent fact; supersede-then-delete must name which
version is removed). The declared rule fixes the outcome; the oracle's permissible set is
derived from it.

### 10.2 The levels

The three levels are the standard lattice restricted to what GMP can express, stated as the
anomalies each **excludes**:

| level          | dirty read | non-repeatable read | phantom | lost update | write skew |
|----------------|:----------:|:-------------------:|:-------:|:-----------:|:----------:|
| read_committed | excluded   | allowed             | allowed | allowed     | allowed    |
| snapshot       | excluded   | excluded            | allowed | excluded    | **allowed**|
| serializable   | excluded   | excluded            | excluded| excluded    | excluded   |

Mapped onto the fact model: **lost update** is a broken supersession chain (the final
`superseded_by` chain skips a committed write — checkable against §3 and validator V3);
**write skew** is two concurrent `supersede`s that each preserve a declared cross-fact
invariant alone but violate it jointly. Write skew **under snapshot** is the diagnostic
cell — it is what separates a genuine `serializable` store from a `snapshot` store that
*claims* `serializable`.

Under immediate-commit (§2), dirty read and dirty write require an uncommitted state the
model does not produce, so **`read_committed` collapses to the observable floor**: its only
proscriptions are vacuous, and it is observationally identical to no-isolation. The two
crisply separated, testable tiers are therefore `snapshot` and `serializable`, with
`read_committed` as the baseline beneath them. Dirty read and full abort/rollback are
**deferred** (no semantic definition exists under immediate-commit; a capability is reserved
only once it has one — §7.4 discipline).

### 10.3 Read-your-own-writes

A transaction's own writes MUST be visible to its own subsequent reads, **regardless of
isolation level** — RYOW sits below the lattice. Under `snapshot` this is the carve-out: a
transaction reads its begin-snapshot *except* its own writes, which override it.

### 10.4 A committed delete is durable under concurrency

This is the one point where the isolation axis meets the §6 privacy axis, and the contact
is a hard constraint, not a free interaction. Visibility of a delete *to a concurrent
reader* is order-dependent (both serial orders are admissible), but the **guarantee** is
invariant across every level: once a delete is in a read's causal past it never resurfaces,
and the permissible-outcome set MUST NOT contain a state in which a committed delete is
undone. The §6.2 `HARD_DELETE` guarantee therefore holds at every isolation level by
construction of the permissible set; a concurrency path that resurrects a committed delete
is a privacy violation, not a permissible ordering.

### 10.5 Coverage contract

`coverage_guarantee` is an agent-readable predicate naming the anomalies the level excludes.
Conformance reports **claimed vs achieved**: the achieved level is the strongest whose
permissible set contains every observed outcome across seeds. A store that claims
`serializable` but exhibits write skew is **downgraded to snapshot** in the report — the
isolation analogue of a store that claims `HARD_DELETE` and leaks (§6.2). The W10 finding is
this claimed-vs-achieved map, not a scalar.

### 10.6 Determinism (R1)

The W10 runner is an **outcome oracle**: it presents a concurrent transaction group, records
what the store commits, and checks membership in the permissible set — it does **not** spawn
real threads (which would be unreproducible). Transaction groups are kept small (2–3 txns)
so the oracle enumerates the admissible serializations deterministically. R1 reproducibility
is preserved.

---

## Appendix A — Traceability

Every normative axis maps to a requirement in the paper, the findings that ground
it, and the reference backend(s) that exercise it (paper Appendix A).

| GMP § | requirement (paper §6) | findings | reference backend(s) |
|---|---|---|---|
| §3 Versioning & validity | R1 — versioning first-class | F3–F5 | `supersession_chain`, `bi_temporal` |
| §4 Retrieval & budget | R2 — embedding-agnostic retrieval | F6–F7 | `vector_only` (+ stub embedder) |
| §5 Retention | R3 — retention declared, not implicit | F8–F9 | `bounded_vector` (K=64) |
| §6 Two-sided privacy | R4 — privacy two-sided, on read path | F10–F13 | `honest`/`soft`/`coarse_delete`; `tenant_scoped`/`leaky_tenant`/`over_isolating` |
| §8 Conformance | R5 — conformance suite is part of the protocol | §5.2 | the suite itself (W1–W6) |
| §1.2, §9 | the open identity question | — | (resolved: tenant-scoped) |

## Appendix B — Reference binding

The Python `MemoryBackend` Protocol (`interface.py`, v0.1.1) is the v0.1 reference
binding. GMP operations map one-to-one onto its seven methods; the nine capability
flags are the `Capability` `StrEnum`; the core types are `Memory`, `SourceMeta`,
`WriteOptions`, `RetrieveOptions`; the error types are `CapabilityNotSupported`
(undeclared operation, §7.2) and `ConformanceViolation` (declared ≠ observed, §8.1);
and `verify_provenance` is the canonical Ed25519 check reserved for §7.4. A future
wire binding (deferred, D1) will encode these operations and types over a transport
without changing the semantics specified here.

---

*GMP v0.1 specifies what a memory store must do; the conformance suite (§8) defines
how a claim to do it is verified. The semantics here are the candidate the paper
deferred; the next artifacts are the runnable conformance suite (§8.2) and a
reference implementation that passes it.*
