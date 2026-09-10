---
status:        proposed
decision_date: —
record_date:   2026-09-10
provenance:    found-scoping-the-standard/product-separation; the code facts below were RE-DERIVED from source on 2026-09-10 rather than carried over from the working note (per the primary-source convention), so the earlier note's phrasing should be diffed against this before acceptance
scope:         repository layout of GNS-Foundation/grafomem — `src/aml/cloud`, `src/aml/static/portal`; the "Grafomem" mark. Counterpart record: Ulissy-s-r-l/eu-governed-agent ADR-0011; parent: ADR-0005
---

# 0012 — Ulissy product code is resident in the Foundation repo **by exception**

## Context

[`eu-governed-agent` ADR-0005](https://github.com/Ulissy-s-r-l/eu-governed-agent/blob/main/docs/decisions/ADR-0005-standard-gns-foundation-products-ulissy.md)
decided that **the standard belongs to GNS-Foundation and the products belong to Ulissy S.R.L.**,
and it did so for a load-bearing reason: a standard owned by the vendor selling against it is not
credibly neutral, and the whole CGR thesis rests on that neutrality ("your reputation isn't signed
by your vendor").

The repository layout does not match the decision. This record states the mismatch, grants a
**time-limited exception** rather than pretending it away, and pins what the Foundation retains
regardless of where the code sits.

## The finding, as verified

Verified against source in `GNS-Foundation/grafomem` at `137dc51` on 2026-09-10. Counts are stated
at the granularity they were measured, because an imprecise count is exactly what this repo's
convention exists to stop.

**1. The repository is PUBLIC and MIT-licensed to the Foundation.**
`gh repo view` reports `visibility: PUBLIC`; `LICENSE` is `MIT License / Copyright (c) 2026 GNS
Foundation`. So the product code below is **already published**, worldwide, under an irrevocable
MIT grant, with a copyright line naming the Foundation rather than Ulissy. This is a fact about
what has already happened, not a risk that separation would create.

It is also **distributed**, not merely published. `pyproject.toml` finds packages under `src/`, so
`aml.cloud` ships inside the public **PyPI `grafomem`** wheel, and `[tool.setuptools.package-data]`
ships `static/portal/*` and `cloud/templates/*.yaml` with it — deliberately, so the portal mounts on
deployed containers. Every release therefore distributes the product code to anyone who runs
`pip install grafomem`.

**2. The resident product code.**

| Path | Extent |
|---|---|
| `src/aml/cloud/` | **82 Python modules at the top level** (`src/aml/cloud/*.py`); 92 tracked files in total, the remainder being 7 `migrations/` and 5 `templates/` files |
| `src/aml/static/portal/` | 3 files — `index.html`, `portal.css`, `portal.js` |

**3. Four import paths run standard -> product** (outside `src/aml/server/`, which is treated
separately below). Every one of them is the *wrong* direction: the Foundation-side code depends on
the Ulissy-side code.

| # | Importer | Target | Binding |
|---|---|---|---|
| 1 | `src/aml/provenance.py:48` | `aml.cloud.identity.SigningIdentity` | **module-level** (hard) |
| 2 | `src/aml/backends/interface.py:97` | `aml.cloud.identity.SigningIdentity` | **module-level** (hard) |
| 3 | `src/aml/cli.py:599,603,606` | `aml.cloud.identity.EnvIdentity` | function-local (lazy) |
| 4 | `src/aml/cgr/validate.py:211` | `aml.cloud.decision_trail.DecisionTrailService` | function-local (lazy) |

Path 4 is the only one inside the **CGR package itself**, and it is the one that blocks shipping a
standalone Python verifier. It is inverted in its own change (`aml.cgr.validate.validate_live` now
takes injected providers; the concrete wiring moved to `aml.cloud.cgr_validate_cli`), with
`tests/test_cgr_import_boundary.py` — a static AST scan, because the import it removed was
*function-local* and no runtime `sys.modules` check would have seen it — holding the line at zero.

**4. `src/aml/server/` is a fifth, much larger surface, deliberately out of scope here.**
`src/aml/server/app.py` alone imports `aml.cloud.*` at roughly seventy sites, plus
`server/auth.py:207` and `server/mcp.py:256,304`. Naming it and excluding it is the point: the
CGR-package boundary is closable in one small change; the server boundary is not, and folding the
two together would have made the small win wait for the large one.

**A caution on the count of four.** The four paths above are the ones re-derived from source today.
The working note that first recorded this finding is not reproduced here verbatim — it was not
available to this record's author — so *four* is a re-derivation that happens to agree with the
figure carried forward, not a transcription of it. If the original four were enumerated differently
(e.g. counting `src/aml/server/` as one path and omitting `cli.py`), the difference is substantive
and should be reconciled before this record is accepted. Flagged rather than smoothed over,
because a number that travels unchecked is precisely the failure this repo's README documents three
times.

## Decision

**1. The exception.** `src/aml/cloud/` and `src/aml/static/portal/` are **Ulissy S.R.L. product
code, resident in the Foundation repository by exception.** They are not Foundation assets and
their presence here does not make them so. The exception is granted because separation is a real
piece of engineering (see the unnumbered spike,
[`separation-of-product-code-design-spike.md`](separation-of-product-code-design-spike.md)) and
because nothing operational is improved by moving the files before the ownership instrument exists.

**2. What the Foundation retains, wherever the code lives.** The Foundation holds and stewards:

- the **specification** — `cgr.attestation.v3`/`v4`, `cgr.cosign.v1`, and successors;
- the **issuer key and identity**;
- the **conformance suite** and the authority to say what conforms;
- **`cgr-verify`** and the reference verifiers;
- the **"Grafomem" mark**.

**3. The mark is licensed, not transferred.** The Foundation licenses the **"Grafomem"** mark to
Ulissy S.R.L. for use in the product name **"GRAFOMEM Cloud"**. A licence, not an assignment: the
mark stays with the neutral body, which is the same reason the issuer key does.

**4. This states INTENDED ownership.** The paragraphs above record what the parties intend to be
true. They are **not** a legal conclusion, and this record does not itself effect any transfer.
**The assignment instrument — and its direction — are for counsel to confirm.** The direction is
genuinely open: whether Ulissy assigns to the Foundation, the Foundation assigns to Ulissy, or the
code is licensed in one direction and never assigned, depends on who authored it, under what
engagement, and on what the already-executed MIT publication (finding 1) did to the options. None
of that is settled by writing it down here.

## Consequences

- **The neutrality claim survives being read closely.** ADR-0005's property is preserved by *what
  the Foundation holds* — spec, issuer key, conformance, verifiers, mark — not by directory
  layout. That is now written down, so the claim no longer rests on the repo tree looking right.
- **The mismatch is on the record instead of in the tree.** A reader who notices 82 product modules
  in the Foundation's public repo now finds a record that names it, rather than an unexplained
  contradiction of ADR-0005. An exception that is documented is a governance artefact; an
  undocumented one is a finding waiting for someone else to make.
- **The four import paths bound how much is really entangled.** Two are module-level (`provenance`,
  `backends/interface`) and both want the same thing — `aml.cloud.identity.SigningIdentity` — which
  suggests a signing-identity interface belongs on the standard side. One is `cli.py` and lazy. One
  is the CGR path, already inverted. The Foundation-side entanglement is small and concentrated;
  `src/aml/server/` is where the weight is.
- **Cost, stated plainly.** The exception buys time and pays for it in a live inconsistency: for as
  long as it stands, the Foundation's public repository ships, under the Foundation's copyright
  line and an MIT grant, code the parties intend to be Ulissy's. Any diligence — an investor's, a
  bank's, an acquirer's — reads the tree before it reads this file. The exception is defensible
  only while it is **time-limited and visibly held**; it stops being defensible the moment it is
  merely the status quo.
- **Nothing here gates bank contact.** Neither this record nor the separation spike is a
  precondition for approaching Iccrea. Said explicitly so the record cannot be read as a hold.

## Open questions

- **The assignment instrument and its direction** — for counsel. Load-bearing and unresolved;
  everything in §4 of the Decision above is intent, not effect.
- **What the executed MIT publication already did.** `src/aml/cloud` has been public under MIT ©
  GNS Foundation for some time. Whether that constrains the assignment, requires a `NOTICE`/
  per-directory copyright correction, or is simply a licence grant the parties are content with, is
  a counsel question and the first one worth asking, because it is the only one that is already
  irreversible for code already released.
- **Mark status.** Whether "Grafomem" is registered anywhere, in whose name, and in which classes —
  unverified here. The licence in §3 is stated as intent over whatever rights exist; it should be
  restated against the register once checked. The **PyPI `grafomem` namespace** and the
  `com.grafomem/*` MCP namespace are adjacent assets with the same unanswered question
  (ADR-0005 lists exactly this under its open sub-questions).
- **Whether the two module-level `SigningIdentity` imports should be closed next.** They are the
  hard ones and they share a target; a signing-identity protocol on the standard side would close
  both. Not scheduled here.
- **The exception's expiry.** Deliberately not set in this draft — an expiry with no owner is
  decoration. It should be set when this record is accepted, together with who reviews it.
