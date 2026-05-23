# GMP Protocol Governance

| Field       | Value                                              |
|-------------|----------------------------------------------------|
| Version     | 0.2.0                                              |
| Date        | 2026-05-24                                         |
| Maintainer  | GNS Foundation                                     |
| Contact     | https://github.com/GNS-Foundation/grafomem/issues  |

---

## 1. Protocol Versioning

GMP follows **semantic versioning** (`MAJOR.MINOR.PATCH`):

- **MAJOR** — breaking protocol changes: new required methods, removed capabilities, incompatible schema changes.
- **MINOR** — additive, non-breaking changes: new optional capabilities, new conformance tests, new benchmark workloads.
- **PATCH** — bug fixes, documentation updates, tooling improvements.

The current protocol version is **0.2.0**.

## 2. Capability Lifecycle

Every capability progresses through four stages:

| Stage        | Meaning                                                                 |
|--------------|-------------------------------------------------------------------------|
| **PROPOSED** | RFC filed as a GitHub Issue; open for community discussion.             |
| **DRAFT**    | Experimental implementation exists; conformance tests written; API may change. |
| **STABLE**   | API locked; conformance tests locked; only backwards-compatible changes allowed. |
| **FROZEN**   | Immutable — the capability's interface and tests will never change.     |

All 10 capabilities defined in v0.2 — including `AUDIT` and `SUPERSESSION_CHAIN` — are **FROZEN**.

## 3. Conformance Suite

The conformance suite is the **source of truth** for protocol compliance.

- A capability is considered *supported* if and only if the corresponding conformance tests pass.
- Tests are **additive**: new tests may be introduced in minor releases, but existing tests are never removed or weakened.
- **M8 (`conformance_rate`)** is the single scalar summary metric. It equals the fraction of applicable conformance assertions that pass.

Run the suite at any time:

```bash
grafomem conformance --backend <your-backend>
```

## 4. Decision Authority

The **GNS Foundation** maintains the canonical specification, the conformance suite, and the reference backend.

Protocol changes follow a four-step process:

1. **RFC** — a GitHub Issue describing the motivation, design, and migration path.
2. **Reference implementation** — a working change against the reference backend.
3. **Conformance tests** — new or updated tests that validate the change.
4. **Review period** — a minimum two-week public review before merge.

Capability *additions* are non-breaking; existing backends are never affected by new optional capabilities. **Breaking changes** (e.g., v1 → v2) require a supermajority vote among active implementors.

## 5. Implementor Rights

- Any project may implement the `MemoryBackend` protocol without prior approval.
- Conformance reports are **self-certifiable** by running `grafomem conformance` against the public suite.
- The GNS Foundation maintains a **public registry** of certified implementations.
- Implementations may omit capabilities. Honest omission is never penalized — the protocol rewards transparency, not feature count.

## 6. Corpus Integrity

The benchmark corpus (`grafomem-bench-v0.2.0`) is **content-addressed and immutable**.

| Property       | Value                      |
|----------------|----------------------------|
| Corpus ID      | `grafomem-bench-v0.2.0`   |
| Content hash   | `f049820b`                 |
| Mutability     | Immutable                  |

Once published, corpus contents never change. If corrections or expansions are needed, a new corpus version must be released with a new content hash. This guarantees that benchmark results remain reproducible across time and across implementations.

---

*This document is maintained by the GNS Foundation and is subject to the same versioning rules it describes.*
