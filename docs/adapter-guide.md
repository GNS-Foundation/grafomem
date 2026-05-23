# Getting Started: Implementing a GMP Backend

This guide walks you through implementing a GRAFOMEM-conformant memory backend.

## 1. Install GRAFOMEM

```bash
pip install grafomem[crypto]
```

## 2. Implement `MemoryBackend`

Create a file (e.g., `my_backend.py`) and implement the 7-method Protocol:

```python
from aml.backends.interface import (
    Capability, Memory, MemoryBackend, MemoryRef,
    RetrieveOptions, WriteOptions,
)


class MyBackend:
    """Your memory store — implements the GMP MemoryBackend Protocol."""

    def capabilities(self) -> set[Capability]:
        """Declare which GMP capabilities your store supports.

        Start with an empty set and add capabilities as you implement them.
        Honest omission is never penalized — the conformance suite only tests
        what you declare.
        """
        return set()  # start here; add as you implement

    def write(self, content: str, opts: WriteOptions = WriteOptions()) -> MemoryRef:
        """Store content and return an opaque reference."""
        ...

    def retrieve(self, query: str, opts: RetrieveOptions = RetrieveOptions()) -> list[Memory]:
        """Return memories relevant to the query, within budget_tokens."""
        ...

    def delete(self, ref: MemoryRef) -> None:
        """Remove a memory. Only called if HARD_DELETE is declared."""
        ...

    def supersede(self, old_ref: MemoryRef, content: str,
                  opts: WriteOptions = WriteOptions()) -> MemoryRef:
        """Replace an old memory with new content. Only called if
        SUPERSESSION_CHAIN is declared."""
        ...

    def audit(self) -> list[Memory]:
        """Return all stored memories. Only called if AUDIT is declared."""
        ...

    def flush(self) -> None:
        """Ensure all writes are visible to subsequent reads."""
        ...
```

## 3. Declare Capabilities

Add capabilities to `capabilities()` as you implement the corresponding behavior:

| Capability | What you implement | When to declare |
|---|---|---|
| `AUDIT` | `audit()` returns everything written | Your store can enumerate all memories |
| `SUPERSESSION_CHAIN` | `supersede()` replaces old content | Your store tracks version chains |
| `BI_TEMPORAL` | `retrieve(as_of=t)` returns historical state | Your store has temporal indexing |
| `HARD_DELETE` | `delete()` fully removes content (no leaks) | Your store guarantees hard deletion |
| `MULTI_TENANT` | `retrieve(tenant_id=X)` scopes results | Your store enforces tenant isolation |
| `PROVENANCE` | `Memory.source` populated on retrieval | Your store tracks data lineage |
| `CRYPTOGRAPHIC_PROVENANCE` | Ed25519 signatures on content | Your store signs memories |

## 4. Run the Conformance Suite

```bash
grafomem conformance -b my_backend:MyBackend -o report.json
```

This runs the GMP §8 conformance suite against your backend. It will:
- Read your `capabilities()` declaration
- Test **only** the capabilities you declared
- Emit a per-capability PASS/FAIL verdict
- Compute M8 (conformance rate — fraction of declared capabilities that pass)

**M8 = 1.0 means your store is conformant.** Every declared capability passes its test.

## 5. Run Workloads

```bash
grafomem run -b my_backend:MyBackend -w W1 W2 W3 -s 5 -d hard
```

This runs the specified workloads and computes M1–M4:
- **M1** (Recall@K) — did your store retrieve the right facts?
- **M2** (Precision@K) — did it avoid junk?
- **M3** (Tokens/fact) — how efficiently did it use the budget?
- **M4** (Latency) — how fast are write/retrieve operations?

## 6. Interpret Results

A backend is deployment-ready when:
- **M8 = 1.0** — all declared capabilities pass conformance
- **M1 ≥ 1.2× persistence baseline** — beats naive last-N-turns by ≥20%
- **Check L = PASS** — zero deletion leakage (if `HARD_DELETE` declared)
- **Check P = PASS** — 100% signature verification (if `CRYPTOGRAPHIC_PROVENANCE` declared)

## 7. Optional: Concurrency Extension

For stores that handle concurrent access, implement `ConcurrentMemoryBackend`:

```python
from aml.backends.interface import (
    ConcurrentMemoryBackend, ConcurrentGroup, ConcurrentResult,
    IsolationPolicy, Capability,
)


class MyConcurrentBackend(MyBackend):
    """Extends MyBackend with concurrency control."""

    declared_policy = IsolationPolicy(...)  # your isolation guarantee

    def capabilities(self) -> set[Capability]:
        return super().capabilities() | {Capability.CONCURRENCY_CONTROL}

    def submit_concurrent(self, group: ConcurrentGroup,
                          policy: IsolationPolicy) -> ConcurrentResult:
        """Execute a concurrent group under the declared policy."""
        ...
```

Then run W10 conformance to verify your isolation claims.

---

*See `02-backend-interface.md` for the full Protocol specification.*
*See `03-eval-metrics.md` for the complete metrics definitions.*
