# Phase 0 Key Custody

This document maintains the mapping of canonical production public keys to their corresponding environments. 

## grafomem-production.up.railway.app
The live production cluster relies on an environment-injected Ed25519 signing key (`ERASURE_SIGNING_KEY`). 

The canonical, bound public key for this production environment is:
`d65d6212368b1ea29b61c30793b236662d89390e66cf0d631e33a59a6fc329cf`

Any execution receipts, memory records, or gcrumbs epochs emitted by the production cluster MUST be signed by the private key corresponding to this identity. Third-party auditors can use this public key to verify the integrity of the data.

## CGR Foundation issuer key (production) — the pin for reputation attestations

Capability-Grounded Reputation (CGR) attestations are signed by a **separate, neutral**
identity — the **Foundation issuer key** — deliberately distinct from `ERASURE_SIGNING_KEY`
above, so an agent's reputation is never signed by the same key that signs the agent's own
records. It is deployment-wide (derived from the production `FOUNDATION_SIGNING_SEED`), **not**
per-tenant.

The canonical, bound **public** key for the production CGR issuer is:

`e7805ce0d5dd06019a2d84c9319baacc1f1516c52ca7d5a0822359918c2893ee`

- **Issuer:** `gns-foundation` · **schema:** `cgr.attestation.v3`
- **This is the pin.** A verifier MUST pin this value **out-of-band** and reject any attestation
  whose `issuer_key_id` does not equal it. Never trust the key an attestation (or a read
  surface) names on the wire.
- **Independent, cross-checkable sources of the same value:**
  - Published pin + offline recipe: <https://docs.grafomem.com/cgr/verify/>
  - Served (for cross-check only, not as the pin): `GET https://api.grafomem.com/v1/cgr/issuer`
- **Key rotation** is out of band: a new Foundation issuer key is announced by re-pinning here
  and on the docs page; agent-identity (subject) rotation is a separate mechanism
  (`GET /v1/cgr/rotations`) and does not change this issuer key.

## Custody model (what this document is and isn't)

This file records **public** keys only — the values third parties pin to verify our signatures.
It contains **no private key or seed material**, by design; committing any would compromise the
identity. The corresponding **private** seeds live only in the production environment
(`FOUNDATION_SIGNING_SEED`, `ERASURE_SIGNING_KEY`) and are governed by the private custody
process (offline backup + custody record + break-glass rotation), which is maintained outside
this repository. Adding or changing a public pin here goes through the governed review loop.

