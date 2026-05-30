# `landing/` — GRAFOMEM v3.0 "Governance Airport"

Open **reference implementation + spec + conformance** for the v3.0 airport: govern the
**landing** of adaptation artifacts and their data provenance (the WHAT), and the
**world-model** the cleared knowledge is shaped into (the HOW), on top of the existing
control tower (the WHICH).

> **Naming note.** This is the governance *landing* (the airport / Landing Certificate).
> It is **not** `src/aml/static/landing/`, which is the marketing landing *page*.

> **Status.** This is a **reference flight**, not production code. The crypto is real
> (BLAKE2b, gcrumbs chain + Merkle epochs + inclusion proofs, Ed25519, governed actions,
> certificate signing + offline verification). The LoRA/RAG artifact is a
> **lineage-complete stub** (Phase-1 tests the runway, not model quality). TRIP is out of
> scope for the doc-memory flight.

## Layout

```
landing/
├── README.md
├── pyproject.toml                  # package: grafomem-landing  (peer of sdk/, adapter_template/)
├── spec/
│   ├── 04-landing-certificate.md    # open spec (lc/0.1)  — candidate to graduate to docs/04-…
│   └── 05-world-model-interface.md  # open spec (wm/0.1)  — candidate to graduate to docs/05-…
├── src/grafomem_landing/
│   ├── hashing.py                   # canon / b2_256 / b2_128  (conventions of src/aml/provenance.py)
│   ├── identity.py                  # Ed25519 + TierGate + GEIANT delegation
│   ├── crumbs.py                    # reference gcrumbs (chain + Merkle + proofs)
│   ├── worldmodel.py                # R5 Object/Link/Action interface + governed actions
│   └── certificate.py               # Landing Certificate issue + offline verify (R3/B1)
└── conformance/
    ├── seed_gns.py                  # the GNS starter world-model (GNS modelling itself)
    ├── gates.py                     # the 10 gates G1–G10, two-sided where safety-relevant
    ├── run_phase1.py                # the dogfood flight
    └── artifacts/                   # generated evidence (cert, chain, dossier, gate report)
```

## Run the dogfood

Standalone (no install):

```bash
python landing/conformance/run_phase1.py            # ingests ./docs, reads ./corpus/corpus.lock
python landing/conformance/run_phase1.py --docs ./docs --out landing/conformance/artifacts
```

Or installed:

```bash
cd landing && pip install -e . && grafomem-landing-dogfood
```

It ingests the repo's documents through customs, builds the GNS world-model, registers a
stub artifact, issues + anchors a Landing Certificate, and runs all ten gates. Exit code
`0` = all green. Evidence lands in `conformance/artifacts/`.

## Open vs. commercial (the Red Hat split)

**Open here:** the two specs, the world-model *interface* + conformance harness, the
Landing Certificate format, and the thin GNS starter ontology. **Commercial (operated):**
the artifact registry, customs, certificate-issuance service, world-model registry +
governed-action runtime, and — the adoption game-changer — the **vertical starter
ontologies** (Utilities, Finance, Defense), which are *not* in this open package.

## Phase B — integrating into the live services (reuse, don't duplicate)

This package is self-contained so the dogfood runs in isolation. In production, each piece
maps onto things that already exist — reuse them:

| Reference module | Production home | Reuse instead of rebuild |
|---|---|---|
| `crumbs.py` (gcrumbs) | — (do **not** add a 2nd impl) | `src/aml/cloud/execution_receipts.py` |
| `identity.py` (Ed25519) / `hashing.py` | — | `src/aml/provenance.py`, `src/aml/wire.py` |
| `worldmodel.py` | `src/aml/cloud/world_model.py` (+ `world_model_routes.py`) | `cloud/policy_engine.py` (PDP) + `cloud/governance.py` (PEP) for the action gate |
| `certificate.py` | `src/aml/cloud/landing_service.py` (+ `landing_routes.py`) | the cert pattern in `cloud/erasure_proof.py` / `cloud/regulatory.py` |
| artifact intake (R1) | `src/aml/cloud/artifact_registry.py` (+ routes) | — |
| `conformance/*` | `tests/landing_self_conformance.py` (next to `tests/gmp_self_conformance.py`) + `src/aml/eval/conformance.py` | the existing two-sided eval harness |
| `spec/04`, `spec/05` | `docs/04-…md`, `docs/05-…md` | the `docs/01–03` numbering convention |
| Phase-2 conflict detection | reuse `src/aml/backends/conflict_backends.py` | the RESERVED-flag scaffolding already present |

New services wire into `src/aml/server/app.py`'s `lifespan()` after the existing ones, with
their own PostgreSQL tables/migrations and matching SDK modules under
`sdk/src/grafomem/services/`. The MCP surface (`src/aml/server/mcp.py`) gains the landing
tools alongside the existing `gns_roll_epoch` / `gns_verify_chain` / `gns_get_compliance_report`.
