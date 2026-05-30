# Phase-1 Landing Self-Conformance — Gate Report

Corpus flown: 5 documents from /mnt/project (real bytes).
Breadcrumbs: 18 · Epochs: 2 · Objects: 26 · Links: 16
Landing Certificate: a77c08b0b164b37492fd5853dd085eeb

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
- **from_where**: corpus f049820bc2450511… sealed in epoch adfe9294278544326104d48f92380fc2
- **under_whom**: camilo.ayerbe@gns at tier release
- **cleared_how**: landing conformance pass (harness landing/0.1)
- **may_do**: ['grafomem_retrieve', 'grafomem_write', 'http_get']