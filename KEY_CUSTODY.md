# Key Custody — Foundation public keys and published pins

This document records the **public** keys third parties pin to verify signatures, and their validity
windows. It is Foundation material under ADR-0005: the standard, the issuer identity, the pins. It
contains **no private key, seed, or credential**, and — by design — **no operational detail of any
deployment** (no variable names, hosts, services, backups, or rotation runbooks). Those belong to the
operating entity's private custody register, maintained outside this repository. Adding or changing a
pin here goes through the governed review loop.

## 1. CGR Foundation issuer key — the pin for reputation attestations

Capability-Grounded Reputation (CGR) attestations are signed by a **separate, neutral** identity — the
**Foundation issuer key** — deliberately distinct from any key the operated runtime uses to sign its own
records, so an agent's reputation is never signed by the same key that signs the agent's records. It is
deployment-wide, **not** per-tenant. Its private seed is held by the Foundation outside this repository.

The canonical, bound **public** key for the production CGR issuer is:

`e7805ce0d5dd06019a2d84c9319baacc1f1516c52ca7d5a0822359918c2893ee`

- **Issuer:** `gns-foundation` · **schema:** `cgr.attestation.v4` *(recorded as `v3` until 2026-09-24;
  production has served `v4` since the v4 vocabulary shipped — label corrected, key unchanged).*
- **This is the pin.** A verifier MUST pin this value **out-of-band** and reject any attestation whose
  `issuer_key_id` does not equal it. Never trust the key an attestation (or a read surface) names on
  the wire.
- **Independent, cross-checkable sources of the same value:**
  - Published pin + offline recipe: <https://docs.grafomem.com/cgr/verify/>
  - Served (for cross-check only, not as the pin): `GET https://api.grafomem.com/v1/cgr/issuer`
- **Key rotation** is out of band: a new Foundation issuer key is announced by re-pinning here and on the
  docs page; agent-identity (subject) rotation (`GET /v1/cgr/rotations`) does not change this key.

## 2. Published keys and validity windows

Public halves only. A verifier's trusted set for a record is the key valid on the record's
`created_at`; retired keys stay listed with their window so historical records remain verifiable.

| Key | Signs | Public key | Served (cross-check only) | Valid from | Valid to |
|---|---|---|---|---|---|
| **CGR Foundation issuer** | `cgr.attestation.v4` | `e7805ce0d5dd06019a2d84c9319baacc1f1516c52ca7d5a0822359918c2893ee` | `GET /v1/cgr/issuer` | v4 issuance | — (current) |
| **Operated-runtime signing key** *(Ulissy S.R.L. — published here as a pin only, see note)* | execution receipts, gcrumbs epochs, erasure certificates; `cgr.cosign.v1` disposition counter-signatures until the dedicated key below is provisioned | `d65d6212368b1ea29b61c30793b236662d89390e66cf0d631e33a59a6fc329cf` | `GET /v1/gcrumbs/public_key`, `GET /v1/gcrumbs/verify/key` | production go-live | — (current) |
| **Disposition signing key** *(Ulissy S.R.L. — decided 2026-09-24; pin pending)* | `cgr.cosign.v1` disposition counter-signatures | *to be recorded at provisioning — staging first, then production* | `GET /v1/dispositions/issuer` (when live) | at provisioning | — |

**Note on the two Ulissy rows.** These are keys of the operated GRAFOMEM Cloud (an Ulissy S.R.L.
product; see `docs/decisions/0012`). Only their **public** pins appear here, because verifiers already
resolve them at this location; their custody — names, services, backups, rotation — is recorded in
Ulissy's private custody register, not in this repository. Canonical publication of Cloud pins moves to
a Ulissy-controlled page when the published-issuer-key work lands; this table will then point there.

## 3. Custody model (what this document is and isn't)

This file records **public** keys only. It contains no private key, seed, pepper, password, or
credential, by design; committing any would compromise an identity. The Foundation's private seed is
governed by the Foundation's custody process outside this repository. Every operational secret of the
operated Cloud is recorded — names, services, backup descriptors, verification and rotation
procedures — in the operating entity's **private** custody register, and none of that detail belongs in
a public file.
