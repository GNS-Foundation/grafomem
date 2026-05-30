# R2 — Data-Provenance Customs (Article 10)

The source end of the pipeline. Register a training-data corpus, run the customs inspection
(refuse to seal sources without a license/lawful basis, or a corpus without a bias examination
and representativeness attestation — EU AI Act Article 10), seal it into a **Merkle root**, and
issue a signed customs receipt. The sealed `merkle_root` + `corpus_hash` are exactly what R3's
landing `data_provenance` block references, so R2 feeds R3:

    R2 seal corpus -> R1 register artifact -> R3 issue certificate (checks the seal) -> R5 govern

Distinctive piece vs the other services: a real Merkle tree over the corpus sources with
O(log n) inclusion proofs — proving a given source is part of the sealed corpus. (These are the
proofs landing's `epoch_anchor` deferred; they live here, at the data layer, where they belong.)

## Where each file goes

| File | Destination |
|---|---|
| `provenance_customs.py` | `src/aml/cloud/provenance_customs.py` |
| `provenance_customs_routes.py` | `src/aml/cloud/provenance_customs_routes.py` |
| `sdk_provenance.py` | `sdk/src/grafomem/services/provenance.py` |
| `provenance_customs_self_conformance.py` | `tests/provenance_customs_self_conformance.py` |

## Wiring — `src/aml/server/app.py`, after the world-model block

```python
from aml.cloud.provenance_customs import ProvenanceCustomsService
from aml.cloud.provenance_customs_routes import create_provenance_customs_router
pc = ProvenanceCustomsService(db_url, signing_key=erasure_key, gateway=gg, decision_trail=dt)
pc.ensure_schema()
app.state.provenance_customs = pc
app.include_router(create_provenance_customs_router(pc))
```

## SDK — `sdk/src/grafomem/client.py`

```python
from grafomem.services.provenance import ProvenanceService
self._provenance = ProvenanceService(self._http)
@property
def provenance(self) -> ProvenanceService:
    return self._provenance
```

## Endpoints (under `/v1/provenance`)

`POST /corpora` (seal) · `GET /corpora` · `GET /corpora/{id}` · `GET /corpora/{id}/verify` ·
`GET /corpora/{id}/proof?source_id=` (Merkle inclusion proof) ·
`GET /corpora/{id}/block` (the data_provenance block for R3) · `GET /stats`.

## How R2 feeds R3

```python
corpus = pc.register_corpus(tenant, CorpusRegisterRequest(name=..., sources=[...], attestations={...}))
block  = pc.provenance_block(tenant, corpus["corpus_id"])   # {merkle_root, corpus_hash, sources, corpus_id}
# then issue the landing certificate with data_provenance=block — it passes R3's "sealed" precondition
```

R3 already refuses to issue a certificate whose `data_provenance` has no `merkle_root`; R2 is
what produces a *real, inclusion-provable* one instead of a placeholder.

## Run the suite

```bash
GRAFOMEM_DB_URL="postgresql://grafomem:grafomem_dev@localhost:5432/grafomem" \
  python3 tests/provenance_customs_self_conformance.py
```

Ten gates, non-vacuous: P4 tampers a sealed source and asserts BOTH the signature and the Merkle
root reject it; P6 proves a member source's inclusion and rejects a non-member and a forged leaf;
P7/P8 refuse to seal data lacking a lawful basis or a bias examination; P10 confirms the block is
landing-ready.

## ADAPT (same small list)

1. `erasure_key` — reuse it (or a dedicated `provenance_key`).
2. `_get_conn()` — per-call; swap to your pool if preferred.
3. SDK transport — align `self._http.*` to `services/erasure.py`.
