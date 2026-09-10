---
status:        proposed
decision_date: —
record_date:   2026-09-10
provenance:    design-spike opened alongside decision 0012; deliberately UNNUMBERED — it scopes work, it does not decide anything
scope:         GNS-Foundation/grafomem — extraction of `src/aml/cloud`, `src/aml/static/portal`, and (the open question) `src/aml/server`
---

# Design spike — separating the product code from the Foundation repo

**Unnumbered on purpose.** Numbers in `docs/decisions/` are permanent and are spent on decisions.
This is a spike: it scopes the work, prices it, and names the one question that has to be answered
before anyone commits. It earns a number if and when it becomes a decision.

> **This does not gate bank contact.** Neither this spike nor
> [0012](0012-product-code-resident-in-the-foundation-repo-by-exception.md) is a precondition for
> approaching Iccrea or any other counterparty. Stated at the top so the record cannot be quoted as
> a hold. The exception in 0012 exists exactly so this work can be sequenced on its merits.

## What is in scope

**Full extraction, with history, plus a deploy retarget.** Specifically:

1. **Extract with history preserved** — `src/aml/cloud/` and `src/aml/static/portal/` (and see the
   open question about `src/aml/server/`) into an Ulissy-owned repository, using
   `git filter-repo --path`-style extraction so the commit history for those paths travels with the
   code rather than landing as one squashed "initial import".
2. **Retarget the deploy** — the Railway service builds this repo's `Dockerfile`, whose `CMD` is
   `grafomem serve`, i.e. `aml.server.app`. After extraction the deployed artefact is built from the
   product repo, with the standard consumed as a dependency.
3. **Retarget the distribution** — `pyproject.toml` currently ships `aml.cloud` and
   `static/portal/*` inside the public PyPI `grafomem` wheel. Extraction means the `grafomem`
   package stops carrying product code, and the product ships as its own artefact.
4. **Invert or interface the remaining standard -> product imports** — the four enumerated in 0012.
   One (`aml.cgr.validate`) is already inverted. Two more (`provenance.py`, `backends/interface.py`)
   are module-level and both want `aml.cloud.identity.SigningIdentity`.

## What is explicitly NOT in scope

- **The `grafomem-web` frontend move.** Relocating `grafomem-web/cloud*` between repos was assessed
  and **rejected as cosmetic**: it moves files without changing the dependency direction, the
  distribution, or who owns what. The dead first-generation `cloud/` directory there was removed on
  its own merits (it was dead), which is a different action with a different justification.
- **`filter-repo` on `grafomem-web`.** Not run, not proposed. The tool is named in step 1 above for
  a *backend extraction that genuinely needs history to travel*; that is not a precedent for
  rewriting the web repo's history, where the same tool would buy nothing.

## The question the spike exists to answer

**Does `src/aml/server/` go with the product, stay with the standard, or split?**

Everything else is mechanical; this is not. The evidence, from 0012's finding 4:
`src/aml/server/app.py` imports `aml.cloud.*` at roughly seventy sites, plus `server/auth.py` and
`server/mcp.py`. In dependency terms `aml.server` is already **downstream of the product** — it is
the thing that wires 82 cloud modules into an application. Three readings, none free:

| Option | Shape | Cost |
|---|---|---|
| **A — server follows the product** | `aml.server` + `aml.cloud` + portal all move to the Ulissy repo; the Foundation keeps `cgr`, `backends`, `provenance`, `generator`, `eval`, the conformance suite and the verifiers | The Foundation repo stops being a runnable server. `grafomem serve` — the thing the README's quickstart and the Dockerfile both invoke — leaves with it. The open-source story becomes "a library and a standard", not "a runtime you can run" |
| **B — server stays, product leaves** | `aml.server` keeps a plugin/entry-point seam; `aml.cloud` registers into it from the product repo | The seventy import sites become the work. This is the honest version of "the server is neutral infrastructure", and it is the expensive one |
| **C — split the server** | A neutral core (`app` factory, auth primitives, MCP surface) stays; the cloud-specific route wiring goes | Most design work, best end state, hardest to bound. Needs the seam from B anyway |

The spike's deliverable is a recommendation between A, B and C **with the seventy sites actually
classified**, not estimated — because the choice turns on how many of them are route registration
(cheap to move) versus genuine coupling (not).

## Sequencing and dependencies

- **Blocks nothing commercial.** See the note at the top.
- **Is not blocked by counsel.** The engineering can be scoped, and the recommendation written,
  while the assignment instrument (0012, open questions) is still with counsel. What it *cannot* do
  is execute: moving code between legal entities before the instrument exists would create the
  record it is meant to fix.
- **Depends on the CGR boundary being held, not re-opened.** The import-boundary test
  (`tests/test_cgr_import_boundary.py`) keeps the standard's core clean while this is decided. If
  that test is ever weakened to unblock something, this spike gets harder, not easier.
- **Interacts with the Python verifier on PyPI.** A verifier that ships without dragging in
  `aml.cloud` is the first concrete payoff of any of this, and it is already reachable now that
  path 4 is inverted — it does not have to wait for the extraction.

## Open questions

- **A, B, or C** — the spike's whole point. Unanswered until the seventy sites are classified.
- **Where the extracted repo lives and what it is called.** Ulissy-owned; name unpicked.
- **What the `grafomem` PyPI package becomes** once it no longer ships `aml.cloud` — and whether
  that is a major version bump for installs that currently rely on the portal mounting.
- **Whether the two module-level `SigningIdentity` imports are closed before or during extraction.**
  Before is cleaner; during is fewer passes over the same code.
- **Cutover shape for the Railway service** — build-from-new-repo with a held rollback to the
  current image, versus any attempt at a live migration. Not designed here; note only that the
  health-gated deploy work is a prerequisite for doing it safely, and is separately gated.
