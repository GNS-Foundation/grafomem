# My GMP Backend

A [GRAFOMEM](https://github.com/GNS-Foundation/grafomem) adapter template.

## Quick Start

```bash
# 1. Install grafomem
pip install grafomem

# 2. Validate Protocol compliance
grafomem check -b my_backend:MyBackend

# 3. Run the conformance suite
grafomem conformance -b my_backend:MyBackend -o report.json
```

## What to Customize

1. **`my_backend.py`** — Replace the in-memory store with your storage system
2. **`capabilities()`** — Add flags as you implement features (delete, tenants, etc.)
3. **`retrieve()`** — Wire in your embedding / search engine

## Capability Progression

| Step | Add to `capabilities()` | Implement | Test |
|---|---|---|---|
| 1 | `AUDIT` (default) | `write`, `retrieve`, `audit` | `grafomem conformance` |
| 2 | `HARD_DELETE` | `delete()` | W6 checks for leakage |
| 3 | `SUPERSESSION_CHAIN` | `supersede()` | W2 checks stale versions |
| 4 | `MULTI_TENANT` | tenant-scoped write/retrieve | W5 checks cross-tenant leakage |
| 5 | `BI_TEMPORAL` | `as_of` in retrieve | W2 historical recall |

See [docs/adapter-guide.md](../docs/adapter-guide.md) for the full walkthrough.
