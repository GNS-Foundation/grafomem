# Phase-1 Landing Self-Conformance — Gate Report

Corpus flown: 9 documents from /Users/camiloayerbeposada/grafomem/docs (real bytes).
Breadcrumbs: 26 · Epochs: 2 · Objects: 30 · Links: 16
Landing Certificate: 564656f82b3a170aa6a2379fc01d2f83

| Gate | Type | Result |
|---|---|---|
| G1 customs coverage | coverage | PASS |
| G2 content/corpus hash | deterministic | PASS |
| G3 world-model validity | structural | PASS |
| G4 action governance (2-sided) | two-sided | PASS |
| G5 cert preconditions (2-sided) | two-sided | PASS |
| G6 independent verification | headline | PASS |
| G7 tamper detection (2-sided) | two-sided | PASS |
| G8 erasure (2-sided) | two-sided | PASS |
| G9 Article-10 projection | coverage | PASS |
| G10 self-sufficiency (offline) | portability | PASS |

## Headline: ALL GREEN — Landing Self-Conformance achieved

### Offline reconstruction (G6) — from cert + chain + keys alone:
- **what**: lora+rag artifact oci://grafomem/gns-assistant:phase1 on base open-weights-7B
- **from_where**: corpus f049820bc2450511… sealed in epoch 19a2e014a1fdc03f7e078685ca5fd48d
- **under_whom**: camilo.ayerbe@gns at tier release
- **cleared_how**: landing conformance pass (harness landing/0.1)
- **may_do**: ['grafomem_retrieve', 'grafomem_write', 'http_get']