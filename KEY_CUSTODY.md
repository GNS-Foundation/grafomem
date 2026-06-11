# Phase 0 Key Custody

This document maintains the mapping of canonical production public keys to their corresponding environments. 

## grafomem-production.up.railway.app
The live production cluster relies on an environment-injected Ed25519 signing key (`ERASURE_SIGNING_KEY`). 

The canonical, bound public key for this production environment is:
`d65d6212368b1ea29b61c30793b236662d89390e66cf0d631e33a59a6fc329cf`

Any execution receipts, memory records, or gcrumbs epochs emitted by the production cluster MUST be signed by the private key corresponding to this identity. Third-party auditors can use this public key to verify the integrity of the data.
