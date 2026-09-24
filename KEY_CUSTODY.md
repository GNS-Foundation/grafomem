# Key Custody — production public pins and the secrets custody register

This document maintains the mapping of canonical production **public** keys to their environments, and
— from 2026-09-24 — a **custody register** for every production secret: what it is named, which
services read it, what it protects, where its offline backup is held (**descriptor only, never the
value, never a path or host**), how the backup is verified against production, and how it is rotated.

**This repository is public.** Nothing here is, or may ever be, secret material: no private key, seed,
pepper, password, URL with credentials, or backup path. Public keys and procedures only. Adding or
changing a pin or a register line goes through the governed review loop.

**Drift corrected 2026-09-24** (against source at `main`; corrections visible, not silent):
the runtime signing key's scope was understated — it also signs **erasure certificates** and, until the
dedicated disposition key ships, **`cgr.cosign.v1` disposition counter-signatures**; the
`GRAFOMEM_SIGNING_KEY` alias was undocumented; the Foundation issuer schema label read `v3` while
production serves `v4`; and no custody record existed for the symmetric secrets or credentials.

---

## 1. Production public pins

### 1.1 Runtime signing key — `ERASURE_SIGNING_KEY` (alias `GRAFOMEM_SIGNING_KEY`, one slot)
The production runtime (`grafomem-production.up.railway.app`) relies on an environment-injected
Ed25519 seed. **Source of truth for the name:** `src/aml/cloud/identity.py:55` reads
`ERASURE_SIGNING_KEY` **or** `GRAFOMEM_SIGNING_KEY` — **one seed slot; `ERASURE_SIGNING_KEY` takes
precedence and the other name is a fallback alias.** Exactly one of the two must be set; if both are set
with different values the second is silently ignored.

The canonical, bound public key for this production identity is:
`d65d6212368b1ea29b61c30793b236662d89390e66cf0d631e33a59a6fc329cf`

It signs, via the single `signing_identity` built in `src/aml/server/app.py:988`: **execution receipts**
(`execution_receipts.py`), **gcrumbs epochs**, **memory/landing records**, **erasure certificates**
(`ErasureProofService`), and — **until the dedicated disposition key (§1.3) is provisioned** —
**`cgr.cosign.v1` disposition counter-signatures** (`disposition_routes.py:164`). Third parties verify
against this key. Served for cross-check (not as the pin): `GET /v1/gcrumbs/public_key`,
`GET /v1/gcrumbs/verify/key`.

### 1.2 CGR Foundation issuer key — `FOUNDATION_SIGNING_SEED` (the pin for reputation attestations)
Capability-Grounded Reputation attestations are signed by a **separate, neutral** identity — the
Foundation issuer — deliberately distinct from the runtime key above, so an agent's reputation is never
signed by the same key that signs the agent's own records. Deployment-wide (`src/aml/cgr/issuance.py`),
**not** per-tenant, and its loader **never falls back** to the runtime slot (`issuance.py:49-60`).

The canonical, bound public key for the production CGR issuer is:
`e7805ce0d5dd06019a2d84c9319baacc1f1516c52ca7d5a0822359918c2893ee`

- **Issuer:** `gns-foundation` · **schema:** `cgr.attestation.v4` *(was recorded as `v3`; production
  has served `v4` since the v4 vocabulary shipped — label corrected 2026-09-24, key unchanged).*
- **This is the pin.** A verifier MUST pin this value **out-of-band** and reject any attestation whose
  `issuer_key_id` differs. Never trust the key an attestation names on the wire.
- Cross-checkable sources: <https://docs.grafomem.com/cgr/verify/> · `GET https://api.grafomem.com/v1/cgr/issuer`.
- **Rotation** is out of band: a new Foundation issuer key is announced by re-pinning here and on the
  docs page; agent-identity (subject) rotation (`GET /v1/cgr/rotations`) does not change this key.

### 1.3 Dedicated disposition signing key — `GRAFOMEM_DISPOSITION_SIGNING_KEY` (decided; pin pending)
Operator decision 2026-09-24 (roadmap r5, S3b(3)): `cgr.cosign.v1` disposition counter-signatures move
to a **dedicated** key, loaded with the Foundation pattern (own env var, fail-closed, no fallback to the
runtime slot), and published at a public issuer endpoint. **Pin: to be recorded here when provisioned —
staging first, then production.** Until then §1.1 is the disposition issuer. Records signed before the
switch keep their `issuer_key_id` and verify under the key that signed them (see §2, validity windows).

### 1.4 Validity windows (so verifiers can build a trusted set)
| Key | Public key | Valid from | Valid to |
|---|---|---|---|
| Runtime (`ERASURE_SIGNING_KEY`) | `d65d6212…329cf` | production go-live | — (current) |
| CGR Foundation (`FOUNDATION_SIGNING_SEED`) | `e7805ce0…2893ee` | v4 issuance | — (current) |
| Disposition (`GRAFOMEM_DISPOSITION_SIGNING_KEY`) | *pending* | at provisioning | — |

A verifier's trusted set for a record is the key valid on the record's `created_at`; retired keys stay
listed here with their window so historical records remain verifiable.

---

## 2. Secrets custody register (all production secrets; values never appear here)
"Services" = whose **code reads** the variable: `grafomem` (the API service) / `grafomem-deamon`
(`python -m aml.cloud.erasure_daemon` — reads only `DATABASE_URL` and, via `SiemExporter`,
`SIEM_WEBHOOK_URL`; holds **no key material**). "Backup" is a **descriptor the operator supplies** —
an entry name in the offline custody store — never a value, path, or host. "Verify" is the read-only
check that the backup equals production **without reading it back from the platform** (sealed
variables cannot be read back). **Rule: no secret is sealed until its backup is verified.**

### 2.1 Asymmetric signing seeds (public halves in §1)
| Variable | Services | Purpose | Encoding | Backup (descriptor) | Verify backup | Rotation |
|---|---|---|---|---|---|---|
| `ERASURE_SIGNING_KEY` (alias `GRAFOMEM_SIGNING_KEY`) | grafomem | §1.1 — receipts, gcrumbs, landing, erasure certificates, dispositions until §1.3 | 32-byte Ed25519 seed, **64 hex chars** (`identity.py:58`) | `[OPERATOR: descriptor]` | derive the public key from the backup seed with `cryptography` (hex→`Ed25519PrivateKey.from_private_bytes`), compare to §1.1 | generate a new seed **offline**; derive and record the new pin + window here; set the platform value **from the backup**; redeploy; `/v1/gcrumbs/public_key` must serve the new pin; keep the old pin listed with its window so historical records verify |
| `FOUNDATION_SIGNING_SEED` | grafomem | §1.2 — CGR attestations | 32-byte seed, 64 hex chars (`issuance.py:50,60`) | `[OPERATOR: descriptor]` | same derivation, compare to §1.2 | as §1.2: new seed offline, re-pin here + docs page, set from backup, redeploy; downstream verifiers (GEIANT) re-pin |
| `GRAFOMEM_DISPOSITION_SIGNING_KEY` (S3b(3)) | grafomem | §1.3 — disposition counter-signatures | 32-byte seed, 64 hex chars | `[OPERATOR: descriptor]` | same derivation, compare to §1.3 pin + `GET /v1/dispositions/issuer` | as §1.1, plus re-pin in the B3 runbook/evidence pack; old records keep their issuer |

### 2.2 Symmetric secrets
| Variable | Services | Purpose | Encoding | Backup (descriptor) | Verify backup | Rotation |
|---|---|---|---|---|---|---|
| `GRAFOMEM_API_KEY_PEPPER` (+ `…_RETIRING` during rotation) | grafomem (auth resolve, mint) **and the pre-deploy migrations runner inside the grafomem service** (backfill) | `api_key_hash = HMAC-SHA256(pepper, api_key)` for every stored API key (`api_key_hash.py:39-45`) | **opaque UTF-8 string, used literally** (`pepper.encode("utf-8")`) — not hex-decoded | `[OPERATOR: descriptor]` | one-time compare: local HMAC-SHA256(backup pepper, one API key the operator holds) vs that key's stored `api_key_hash` (read-only `SELECT`); key must be hashed under the **current** pepper | **dual-pepper window** (design: `grafomem-internal/design/2026-09-19-hash-keys-at-rest.md` §"Pepper rotation procedure"): set `…_RETIRING` = current, set current = new, deploy; re-mint every key (rotation sweep; `pepper_version` per row targets the stragglers); drop `…_RETIRING` only when the sweep is complete. **Losing the pepper with no backup = total API auth lockout until every key is re-minted.** |
| `GRAFOMEM_MASTER_KEY` | grafomem (`app.py:900`; `tenant_key_manager.py:33-38`; `invoice_pseudonym.py`) | wraps tenant data-encryption keys (KEK); keys the per-tenant invoice-ref pseudonym HMAC (purpose-separated) | **≥32-byte key as ≥64 hex chars**; first 32 bytes used (`tenant_key_manager.py:34-38`) | `[OPERATOR: descriptor]` | functional: `aml.cloud.invoice_pseudonym.pseudonymize(<known raw ref>, <tenant_id>, master_key_hex=<backup>)` must reproduce a stored `OUT-…` pseudonym (read-only `SELECT`); a PII-free DEK-unwrap check is preferable but not yet written | **no rotation procedure exists in source** (no rewrap/rotate tooling in `tenant_key_manager.py`). Rotation would require re-wrapping every tenant DEK **and** changes every invoice pseudonym (breaks the equality join). **Design required before any rotation — backlog.** **Losing it with no backup = tenant data unrecoverable.** |
| `PROVIDER_ENCRYPTION_KEY` | grafomem (`identity.py:64-76`) | LLM provider credentials at rest | **Fernet key(s), urlsafe-base64, comma-separated** (`MultiFernet`) — newest first | `[OPERATOR: descriptor]` | non-revealing: `Fernet(backup).extract_timestamp(<one stored ciphertext>)` succeeds only if the key matches; no plaintext produced | `MultiFernet` rotation: **prepend** the new key to the comma list, deploy, re-encrypt stored tokens (`MultiFernet.rotate`; a sweep tool is **not** in source — backlog), then drop the old key |
| `GRAFOMEM_PORTAL_SECRET` | grafomem (`portal_auth.py:154`) | portal session/JWT signing | opaque string | `[OPERATOR: descriptor]` | set-from-backup at sealing (nothing to derive) | set a new value; all portal sessions are invalidated (users re-login) |

### 2.3 Credentials (custodied and rotated; **not sealed** per S3b(4))
| Variable | Services | Purpose | Encoding | Backup (descriptor) | Verify | Rotation |
|---|---|---|---|---|---|---|
| `DATABASE_URL` | **both** (`erasure_daemon.py:107`, required) | primary Postgres | URL with credential | `[OPERATOR: descriptor]` | set-from-backup; `/readyz` 200 after deploy | Railway guide "Rotate API keys and database credentials without downtime" (`docs.railway.com/guides/rotate-credentials-zero-downtime`) |
| `GRAFOMEM_DB_URL`, `GRAFOMEM_DB_READ_URL` | grafomem | app / read-replica | URL | `[OPERATOR: descriptor]` | as above | as above |
| `GRAFOMEM_LEDGER_URL` | grafomem | `grafomem_ledger` role → `erasure_ledger` (I0) | URL | `[OPERATOR: descriptor]` | ledger pool authenticates at boot (`ensure_schema:ErasureLedger` = permission-denied, not timeout) | as above; role-scoped |
| `GRAFOMEM_MIGRATE_URL` | grafomem (pre-deploy runner) | migrate role | URL | `[OPERATOR: descriptor]` | pre-deploy migration step succeeds | as above; role-scoped |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | grafomem (`stripe_billing.py`, `portal_routes.py`) | billing API; webhook signature | `sk_live_…`, `whsec_…` | `[OPERATOR: descriptor]` | set-from-backup; a read-only Stripe call | roll in the Stripe dashboard, then set |
| `APNS_AUTH_KEY` + `APNS_KEY_ID` | grafomem (`push_service.py:25-26`) | APNs push signing (EC P-256 `.p8`; public half held by Apple) | opaque string — accepted PEM/base64 form **not confirmed from source** | `[OPERATOR: descriptor]` | set-from-backup; a push-token registration | new key in the Apple developer portal; set both |
| `GRAFOMEM_TOKENS` | grafomem (`auth.py:92`, legacy token mode) | static bearer→tenant map — secret **if set**; expected unset in cloud mode | JSON string | `[OPERATOR: confirm unset]` | — | remove if unset; else rotate the map |
| `SIEM_WEBHOOK_URL` | grafomem-deamon (`siem_exporter.py:15`) | SIEM destination — **currently unset** | URL | `[OPERATOR: when set]` | export succeeds | rotate at the destination; seal when set |

*Not secrets (never seal):* `GRAFOMEM_API_KEY_DUAL_READ`, `GRAFOMEM_API_KEY_LOGIN_DROP` (flags),
`GRAFOMEM_VERIFY_URL`, `GRAFOMEM_FRONTEND_URL` (public URLs).

## 3. Custody model (what this document is and isn't)
This file records **public** keys, names, procedures, and backup **descriptors** only. It contains no
private key, seed, pepper, password, or credential, by design; committing any would compromise the
identity. The private material lives only in the production environment and in the operator's offline
custody store, governed by the private custody process (offline backup + custody record + break-glass
rotation) maintained outside this repository. **Sealing order (S3b):** register complete → every backup
verified per §2 → dedicated disposition key provisioned and pinned (§1.3) → seal signing keys, pepper,
master key (not DB URLs) → read-only check: `/readyz` 200 and every served key equals its pin.
