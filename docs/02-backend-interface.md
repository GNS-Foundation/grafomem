# GRAFOMEM — Backend Interface Specification v0.1.1

| Field | Value |
|---|---|
| **Status** | Draft |
| **Schema version** | 0.1.1 |
| **Last updated** | 2026-05-19 |
| **Authors** | Camilo Ayerbe Posada · Claude (engineering partner) |
| **Companion docs** | `01-workload-spec.md` (v0.1.1) · `03-eval-metrics.md` (forthcoming) |

---

## Changelog

**v0.1.1 (2026-05-19)** — Crypto-provenance primitives + embedding methodology.

- Added `CRYPTOGRAPHIC_PROVENANCE` capability flag (§3) — independent of `PROVENANCE`.
- Added `signature: bytes | None` and `public_key: bytes | None` fields to `SourceMeta` (§4).
- Defined canonical signed payload: **Ed25519 signature over the 16-byte `fact_id`** (§4.1).
- Added conformance check for `CRYPTOGRAPHIC_PROVENANCE` (§10).
- Resolved open decision: `flush()` remains always-required (§5.7, §12.2).
- Resolved open decision: embedding choice is backend-internal, but reference adapters MUST pin `BGE-small-en-v1.5` (§9.1).
- Added adapter-metadata requirement so non-reference adapters declare their embedding model — separates architectural from implementation claims in findings (§9.2).
- Capability count: 8 → 9.

---

## 1. Scope

This document specifies:

- The `MemoryBackend` Protocol that every architecture-under-test must implement.
- The capability flag system — how backends declare what they support.
- The capability × workload matrix — how the eval harness adapts scoring to declared capabilities.
- The canonical scheme for cryptographic provenance over memories.
- The adapter pattern — how existing systems (Letta, Zep, mem0, etc.) are wrapped into the Protocol.
- A conformance-suite outline that validates a backend's declared capabilities match its actual behavior.

This document does **not** specify:

- Trace data model — see `01-workload-spec.md` §3.
- Workload generation procedures — see `01-workload-spec.md` §4.
- Evaluation metrics (M1–M7) — see `03-eval-metrics.md` (forthcoming).
- Implementation of any specific backend adapter — those live in `src/aml/backends/<name>.py`.

---

## 2. Design principles

**B1 — Capabilities are declared, not inferred.** Every backend declares a set of `Capability` flags. The eval harness adapts to the declaration; it does not punish backends for honest omissions. A backend that does not claim `BI_TEMPORAL` is not penalized for failing pre-supersession queries — those queries are marked N/A for that backend's W2 run.

**B2 — Conformance precedes performance.** Before a backend's results are accepted into a published finding, it must pass the conformance suite (§10) that verifies its actual behavior matches its declared capabilities. A backend that claims `HARD_DELETE` but fails the deletion-leakage conformance check is rejected outright — the result is not "low score on W8," it's "non-conformant, results invalid."

**B3 — The interface is minimal but sufficient.** Seven methods. Any additional surface area (batching, health checks, snapshots) is layered above the core Protocol via mixins or adapter helpers, never required in v0.1.x.

**B4 — Hard delete is honest deletion.** `delete()` is a contractual hard wipe: post-`delete`, the deleted `MemoryRef` MUST NOT be recoverable through `retrieve()`, `audit()`, or any other interface surface. A backend that retains a soft-delete shadow MUST NOT claim `HARD_DELETE`.

**B5 — `MemoryRef` is opaque to the harness.** Backends define their own `MemoryRef` (UUID, content hash, hierarchical path — whatever they need internally). The harness treats refs as opaque tokens whose only operation is equality-comparison and pass-through.

**B6 — Cryptographic primitives are first-class but optional.** Memory attestation via Ed25519 signatures is part of the v0.1.x data model. Backends that don't sign their memories simply leave the relevant fields `None` and don't claim the corresponding capability. Backends that *do* sign use the canonical scheme defined in §4.1; no proprietary variants. This keeps the framework forward-compatible with the regulatory and federation arcs without forcing crypto on adapters that don't need it.

---

## 3. Capability flags

The capability set is enumerated. A backend declares which it supports via `capabilities() -> set[Capability]`.

```python
class Capability(StrEnum):
    BI_TEMPORAL                = "bi_temporal"
    HARD_DELETE                = "hard_delete"
    SUPERSESSION_CHAIN         = "supersession_chain"
    CROSS_SESSION_PROPAGATION  = "cross_session_propagation"
    MULTI_TENANT               = "multi_tenant"
    CONFLICT_DETECTION         = "conflict_detection"
    PROVENANCE                 = "provenance"
    CRYPTOGRAPHIC_PROVENANCE   = "cryptographic_provenance"
    AUDIT                      = "audit"
```

**Semantics:**

| Flag | Meaning |
|---|---|
| `BI_TEMPORAL` | `retrieve()` accepts an `as_of: datetime` parameter and returns the world-state as it stood at that time. Required for W2 pre-supersession queries. |
| `HARD_DELETE` | `delete()` permanently destroys the referenced memory. Post-`delete`, the ref is unrecoverable via any interface. Required for W8 (deferred). |
| `SUPERSESSION_CHAIN` | `supersede()` is supported and tracks the explicit linkage between old and new memories. Required for W2 historical queries. |
| `CROSS_SESSION_PROPAGATION` | Writes, supersessions, and deletions in one session are immediately visible to other sessions of the same tenant. Required for W8. |
| `MULTI_TENANT` | The backend honors a `tenant_id` parameter and enforces isolation. Required for W5. |
| `CONFLICT_DETECTION` | Concurrent writes to the same logical fact surface as a detectable signal. Required for `conflict_flag` classification in W6. |
| `PROVENANCE` | Each retrieved `Memory` carries source metadata (`write_id`, `written_at`, `written_by`). **Bookkeeping-grade attestation.** |
| `CRYPTOGRAPHIC_PROVENANCE` | Each retrieved `Memory` carries a verifiable Ed25519 signature over its `fact_id`, plus the signer's public key. **Attestation-grade.** |
| `AUDIT` | `audit()` is implemented and returns the full retrievable memory set (subject to B4 — must not surface deleted refs). |

Flags are **independent**. A backend can claim `CRYPTOGRAPHIC_PROVENANCE` without `PROVENANCE` (signatures but no source metadata), or vice versa, or both, or neither. In practice they typically come together — that's an empirical observation about the field, not a normative requirement.

---

## 4. Core types

```python
from typing import Protocol, runtime_checkable, TypeVar
from datetime import datetime
from enum import StrEnum
from collections.abc import Iterator

# Opaque to the harness. Backend defines its own concrete type.
MemoryRef = TypeVar("MemoryRef")

class Memory:
    ref:           MemoryRef
    content:       str
    metadata:      dict
    written_at:    datetime                # backend's record of the write time
    valid_from:    datetime | None         # if BI_TEMPORAL, else None
    valid_until:   datetime | None         # if BI_TEMPORAL, else None
    tenant_id:     str | None              # if MULTI_TENANT, else None
    superseded_by: MemoryRef | None        # if SUPERSESSION_CHAIN, else None
    source:        SourceMeta | None       # if PROVENANCE or CRYPTOGRAPHIC_PROVENANCE

class SourceMeta:
    # Populated if PROVENANCE is claimed
    write_id:      str | None              # backend-internal write identifier
    written_at:    datetime | None
    written_by:    str | None              # session_id or agent_id, if available
    
    # Populated if CRYPTOGRAPHIC_PROVENANCE is claimed
    signature:     bytes | None            # Ed25519 signature over fact_id (64 bytes)
    public_key:    bytes | None            # Ed25519 public key (32 bytes)

class WriteOptions:
    valid_from:    datetime | None         # honored if BI_TEMPORAL
    tenant_id:     str | None              # honored if MULTI_TENANT
    signing_key:   bytes | None            # if set, backend MUST sign (and MUST claim CRYPTOGRAPHIC_PROVENANCE)
    metadata:      dict

class RetrieveOptions:
    budget_tokens: int                     # hard cap on returned content size
    as_of:         datetime | None         # honored if BI_TEMPORAL; default = now
    tenant_id:     str | None              # honored if MULTI_TENANT
    top_k:         int | None              # backend-specific hint; not contractual
```

### 4.1 Canonical signing scheme

The `signature` field, when present, MUST be an **Ed25519 signature over the 16-byte `fact_id`** as defined in `01-workload-spec.md` §3.2. The `fact_id` is itself a `BLAKE2b-128` digest over the canonical fact tuple, so signing it produces a verifiable attestation of "this signer asserts the existence of this fact" without requiring a second canonicalization specification.

**Verification procedure** (harness-side, deterministic):

```python
def verify_provenance(memory: Memory, expected_fact_id: bytes) -> bool:
    if memory.source is None or memory.source.signature is None:
        return False
    if memory.source.public_key is None:
        return False
    if memory.ref_to_fact_id() != expected_fact_id:
        return False
    return ed25519_verify(
        public_key=memory.source.public_key,
        message=expected_fact_id,
        signature=memory.source.signature,
    )
```

**Algorithm pinning:** Ed25519 only in v0.1.x. Adding additional signature algorithms (e.g., post-quantum schemes) is a minor-version event with a corresponding new capability flag (e.g., `CRYPTOGRAPHIC_PROVENANCE_PQ`). Backends MUST NOT use other algorithms under the existing flag.

**Key rotation and revocation** are out of scope for v0.1.x. A backend with rotated keys re-signs on supersession; deleted memories cannot have their signatures revoked retroactively, which is consistent with the cryptographic-shredding property of §3.8 in the workload spec.

---

## 5. The `MemoryBackend` Protocol

```python
@runtime_checkable
class MemoryBackend(Protocol):

    def capabilities(self) -> set[Capability]:
        """Return the set of capabilities this backend supports."""
        ...

    def write(self, content: str, options: WriteOptions) -> MemoryRef:
        """Persist a new memory and return its opaque ref.
        
        If options.signing_key is set, the backend MUST sign per §4.1
        and MUST have claimed CRYPTOGRAPHIC_PROVENANCE.
        """
        ...

    def supersede(
        self,
        old_ref: MemoryRef,
        content: str,
        options: WriteOptions,
    ) -> MemoryRef:
        """Replace old_ref with a new memory while preserving history.
        
        Requires SUPERSESSION_CHAIN. If unsupported, MUST raise
        CapabilityNotSupported.
        """
        ...

    def delete(self, ref: MemoryRef) -> bool:
        """Hard-delete the referenced memory.
        
        Requires HARD_DELETE. Post-call, ref MUST NOT be recoverable via
        retrieve() or audit(). Returns True on success, False if the ref
        was not found. MUST NOT raise on already-deleted refs.
        """
        ...

    def retrieve(
        self,
        query: str,
        options: RetrieveOptions,
    ) -> list[Memory]:
        """Return memories relevant to query, respecting budget_tokens.
        
        If as_of is set and BI_TEMPORAL is not claimed, MUST raise
        CapabilityNotSupported. If tenant_id is set and MULTI_TENANT is
        not claimed, MUST raise CapabilityNotSupported.
        """
        ...

    def audit(self) -> Iterator[Memory]:
        """Iterate all retrievable memories, including superseded ones,
        EXCLUDING hard-deleted ones.
        
        Requires AUDIT. Used by the eval harness for the deletion-leakage
        check (§8) and the W6 behavior classification.
        """
        ...

    def flush(self) -> None:
        """Block until all preceding writes/supersedes/deletes are durable.
        
        Always required. No-op for in-memory backends. The eval harness
        calls flush() before every retrieve() to guarantee read-after-write
        consistency, which eliminates a class of timing-dependent test
        flakes in multi-seed sweeps.
        """
        ...
```

Errors and exceptions:

```python
class CapabilityNotSupported(Exception):
    """Raised when an operation requires a capability the backend doesn't claim."""
    def __init__(self, capability: Capability, operation: str): ...

class ConformanceViolation(Exception):
    """Raised by the conformance suite when declared capabilities don't match behavior."""
    ...
```

---

## 6. Method specifications

### 6.1 `capabilities()`

- **Returns:** A set of `Capability` flags this backend supports.
- **Invariant:** The set MUST be stable across the backend's lifetime — it is read once by the harness at setup.
- **Conformance:** Each declared capability is verified by a dedicated conformance test (§10).

### 6.2 `write(content, options) -> MemoryRef`

- **Always required.** Every backend supports basic write.
- **Behavior under `BI_TEMPORAL`:** `options.valid_from` is honored.
- **Behavior without `BI_TEMPORAL`:** `options.valid_from` is silently ignored; the memory is retrievable immediately.
- **Behavior under `MULTI_TENANT`:** `options.tenant_id` is honored; the memory is isolated to that tenant's namespace.
- **Behavior without `MULTI_TENANT`:** `options.tenant_id` MUST be `None` or `CapabilityNotSupported` is raised.
- **Behavior under `CRYPTOGRAPHIC_PROVENANCE`:** If `options.signing_key` is set, the backend signs per §4.1 and populates `source.signature` and `source.public_key` on retrieval.
- **Returns:** An opaque `MemoryRef`.

### 6.3 `supersede(old_ref, content, options) -> MemoryRef`

- **Requires:** `SUPERSESSION_CHAIN`.
- **Behavior:** The new memory replaces `old_ref` semantically. The old memory remains discoverable via `audit()` and (if `BI_TEMPORAL`) via `retrieve(as_of=t < supersession_time)`.
- **Linkage:** The new memory's `superseded_by` field is `None` until itself superseded. The old memory's `superseded_by` field now points to the new ref.
- **Re-signing:** If `CRYPTOGRAPHIC_PROVENANCE` is claimed and `options.signing_key` is set, the new memory carries a fresh signature over its own `fact_id`. The old memory's signature is preserved unchanged.
- **If `old_ref` not found:** Behaves as a fresh `write` (the ref is no longer linked to anything), with a structured warning logged.

### 6.4 `delete(ref) -> bool`

- **Requires:** `HARD_DELETE`.
- **Behavior:** Permanent destruction. Post-`delete`:
  - `retrieve()` MUST NOT return the deleted memory under any query, `as_of`, or `tenant_id`.
  - `audit()` MUST NOT yield the deleted memory.
  - Supersession chains containing the deleted ref are repaired per `01-workload-spec.md` §3.8 Rule O2.
- **Cryptographic residue:** Deletion destroys the signature along with the memory. There is no "tombstone signature" — that would defeat the cryptographic-shredding property. The fact that a deletion occurred at time T is still inferable from surviving supersession chain timestamps (as noted in workload spec §3.8), but the deleted content and its attestation are unrecoverable.
- **Returns:** `True` if the ref was found and deleted; `False` if the ref was not found.

### 6.5 `retrieve(query, options) -> list[Memory]`

- **Always required.**
- **Budget enforcement:** The cumulative `len(m.content)` (in characters as a token-proxy, or true tokens if the backend has access to the tokenizer) of returned memories MUST NOT exceed `options.budget_tokens`. The backend chooses which memories to drop when over budget; this is the policy under test.
- **Ordering:** Backend-determined relevance order.
- **Determinism:** For fixed backend state and fixed query, repeated `retrieve()` MUST return the same list. Stochastic retrieval is permitted only if seeded externally.

### 6.6 `audit() -> Iterator[Memory]`

- **Requires:** `AUDIT`.
- **Yields:** Every retrievable memory the backend holds, INCLUDING superseded ones, EXCLUDING hard-deleted ones.
- **Order:** Unspecified; harness sorts as needed.

### 6.7 `flush() -> None`

- **Always required.**
- **Contract:** After `flush()` returns, all preceding mutations are durable and visible to subsequent `retrieve()` and `audit()` calls on this and any other session of the same backend instance.
- **Rationale:** Three lines of `pass` for synchronous backends is a microscopic tax for an airtight execution harness. Making it always-required forces every adapter implementer to think about read-after-write consistency from line one, and it eliminates a class of timing-dependent flakes that would otherwise compound across multi-seed sweeps.

---

## 7. Capability × workload matrix

How the eval harness adapts to each backend's declared capabilities.

| Workload | Required capabilities | Capability-affected scoring |
|---|---|---|
| **W1** Stable Recall | — | None. Pure retrieval baseline. |
| **W2** Drift & Conflict | `SUPERSESSION_CHAIN` | If `BI_TEMPORAL` absent: pre-supersession queries marked N/A; post-supersession + ambiguous queries scored normally. |
| **W3** Distractor Noise | — | None. |
| **W4** Long-Horizon | — | None. |
| **W5** Multi-Tenant Isolation | `MULTI_TENANT` | If absent: workload skipped (backend marked "single-tenant only"). |
| **W6** Concurrent Updates | — | If `CONFLICT_DETECTION` absent: backend cannot achieve `conflict_flag` classification; defaults to observed behavior class. |
| **W7** Forgetting Curve *(v0.2)* | — | TBD. |
| **W8** Right to Be Forgotten *(v0.2)* | `HARD_DELETE`, `CROSS_SESSION_PROPAGATION` | If either absent: workload skipped. |

The deletion-leakage check (§8) and the cryptographic-provenance check (§8) are **always** run, regardless of workload, on backends that claim the relevant capability.

---

## 8. Always-on safety checks

Two checks run on every backend's output, regardless of which workload is being evaluated:

**Check L — Deletion leakage.** Run on every backend that claims `HARD_DELETE`. For each fact in `GroundTruth.deleted_facts`, probe every retrieval surface (`retrieve` with multiple query phrasings; `audit` if claimed). Any reappearance of a deleted ref is a leak. Backends with leaks have their `HARD_DELETE` claim revoked and the relevant workload results invalidated.

**Check P — Provenance verification.** Run on every backend that claims `CRYPTOGRAPHIC_PROVENANCE`. For every retrieved `Memory`, verify the Ed25519 signature against the `fact_id` and the supplied `public_key`. Any signature that fails to verify is a contract violation. Surfacing a `Memory` with `signature=None` while claiming the capability is also a violation.

Both checks produce structured reports separate from workload scores, so a backend can score 100% on W1 and still fail either safety check — and the failure is the headline finding.

---

## 9. Adapter pattern

Existing systems (Letta, Zep, mem0, LangGraph, OpenAI memory, etc.) have their own APIs. The LAB wraps each in an **adapter** that exposes the `MemoryBackend` Protocol and declares the capabilities the underlying system actually supports.

### 9.1 Reference adapters

The LAB ships reference adapters used as scientific baselines. These MUST pin specific implementation details to keep the baseline a controlled variable:

- **`vector_only`** — FAISS or Qdrant + `BGE-small-en-v1.5` (pinned). Reference baseline.
- **`vector_graph`** — Same vector layer (BGE-small pinned) + extracted relational graph.

Both ship at known commit hashes of their dependencies, recorded in `corpus.yaml` alongside each finding.

### 9.2 Non-reference adapters

Wrappers around third-party systems use whatever embedding/retrieval the underlying system natively does. They MUST declare their implementation choices in an `__grafomem_adapter_metadata__` attribute:

```python
__grafomem_adapter_metadata__ = {
    "underlying_system": "zep",
    "underlying_version": "1.x.x",
    "embedding_model": "openai-text-embedding-3-small",
    "vector_store": "internal-zep-pgvector",
    "notes": "Zep uses native bi-temporal graph; embedding is configurable but defaults shown.",
}
```

**Why this matters.** Findings of the form "Zep beat vector_only on W1 by X%" are confounded if Zep uses a stronger embedding than the baseline. The metadata declaration lets us write honest findings: "Zep beat vector_only on W1 by X%, but Zep uses `text-embedding-3-small` while the baseline uses `BGE-small-en-v1.5`. A controlled re-run with both forced to BGE-small shows Y%." That kind of finding is publishable; the un-disambiguated version isn't.

Phase 1 runs adapters with their native defaults. Phase 2 may run controlled-embedding reruns of the top adapters where the architectural delta is the publishable claim. Decision deferred to post-Phase-3 findings.

### 9.3 Initial capability claims

Provisional — subject to conformance adjudication:

| Adapter | Underlying system | Initial capability claim |
|---|---|---|
| `vector_only` | FAISS + BGE-small (reference baseline) | `{AUDIT}` |
| `vector_graph` | mem0-style hybrid | `{HARD_DELETE, AUDIT}` |
| `paged_hierarchy` | Letta-inspired | `{SUPERSESSION_CHAIN, AUDIT}` |
| `bi_temporal_graph` | Zep / Graphiti-inspired | `{BI_TEMPORAL, SUPERSESSION_CHAIN, AUDIT}` |
| `reflection_plus` | Generative Agents (Park et al.) | `{SUPERSESSION_CHAIN, AUDIT}` |
| `hybrid_candidate` | LAB contribution, post-Phase-3 design | TBD; expected to claim `{BI_TEMPORAL, SUPERSESSION_CHAIN, HARD_DELETE, CROSS_SESSION_PROPAGATION, PROVENANCE, CRYPTOGRAPHIC_PROVENANCE, AUDIT}` |

None of the existing players currently claim `CRYPTOGRAPHIC_PROVENANCE`. That gap is the most interesting one in the matrix and is what the `hybrid_candidate` adapter is designed to fill.

---

## 10. Conformance suite

A separate test pack run **before** any workload. Each capability has a dedicated test.

| Capability | Conformance check |
|---|---|
| `BI_TEMPORAL` | Write F at t=1 with valid_from=10, valid_until=20. `retrieve(as_of=15)` MUST return F; `retrieve(as_of=25)` MUST NOT. |
| `HARD_DELETE` | Write F, delete F, probe via every retrieval surface (multiple queries, `audit()`, `retrieve(as_of=any)`). F MUST be unrecoverable. |
| `SUPERSESSION_CHAIN` | Write F1, supersede → F2. `audit()` MUST yield both; F1.superseded_by MUST equal F2.ref; retrieve "current" MUST return F2 only. |
| `CROSS_SESSION_PROPAGATION` | Open two backend handles to the same instance. Write F via A, delete F via A, retrieve via B. F MUST NOT appear. |
| `MULTI_TENANT` | Write F with tenant_id="A". `retrieve(tenant_id="B", query=anything)` MUST NOT return F. |
| `CONFLICT_DETECTION` | Two overlapping writes to the same `(predicate, subject)` with different `object`. At least one of: a conflict exception is raised, a conflict marker appears in retrieval results, or `audit()` exposes both. |
| `PROVENANCE` | After write with metadata `{session_id: "X"}`, retrieve and verify `m.source.written_by == "X"`. |
| `CRYPTOGRAPHIC_PROVENANCE` | Generate Ed25519 keypair (sk, pk). Write F with `options.signing_key=sk`. Retrieve F. Verify `m.source.signature` and `m.source.public_key` populated. Verify `ed25519_verify(pk, fact_id, signature) == True`. Then mutate one bit of `signature` and confirm verification fails. |
| `AUDIT` | After 10 writes, `audit()` yields exactly 10 entries with matching content. |

A backend that fails any claimed capability's conformance test is rejected. The harness reports: *"Backend `mem0_adapter` claims `HARD_DELETE` but the deletion conformance check found the ref recoverable via audit. Backend rejected pending capability re-declaration."*

This adjudication is itself a finding — generated before any workload runs.

---

## 11. Versioning and extension

- Capability flags are append-only across spec versions. Existing flags MUST NOT change semantics.
- Adding a new flag is a minor-version bump (`0.1.x → 0.2.0`).
- Tightening an existing flag's contract (making it stricter) is a major-version bump.
- Adding a new signature algorithm (e.g., post-quantum) is a minor-version bump and introduces a new capability flag (e.g., `CRYPTOGRAPHIC_PROVENANCE_PQ`) — the existing `CRYPTOGRAPHIC_PROVENANCE` flag remains Ed25519-only.
- Backends declare which interface version they target via a `__grafomem_interface__: str` attribute.

---

## 12. Resolved decisions (from v0.1.0 §12)

| Decision | Resolution |
|---|---|
| Crypto signature field on `SourceMeta` | **Added.** Plus `public_key` and a new `CRYPTOGRAPHIC_PROVENANCE` capability flag. Ed25519 over `fact_id`. |
| `flush()` always-required vs. capability-gated | **Always required.** Microscopic implementation tax; eliminates timing flakes in multi-seed sweeps. |
| Embedding pinning at interface level | **Backend-internal**, but reference adapters pin `BGE-small-en-v1.5` and non-reference adapters declare their embedding in `__grafomem_adapter_metadata__`. |

---

## 13. Next deliverables

1. `03-eval-metrics.md` — M1–M7 mathematical definitions + W6 capability map + leakage scoring + provenance verification scoring.
2. `src/aml/backends/interface.py` — Python Protocol + types + exception classes (including `Capability`, `SourceMeta`, `MemoryBackend`).
3. `src/aml/backends/conformance.py` — the conformance test pack from §10.
4. `src/aml/backends/vector_only.py` — first reference adapter, declaring `{AUDIT}`. Pinned to BGE-small-en-v1.5.
5. Conformance suite green on `vector_only`.
6. Checkpoint commit; proceed to remaining adapters.

---

*End of document.*
