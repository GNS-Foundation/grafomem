# cgr.disposition.v1 runtime conformance corpus

The executable target for the `cgr.disposition.v1` runtime record class + the
`POST /v1/dispositions` attest route and `GET /v1/dispositions/{id}` verify route — the
prerequisite grafomem work that unblocks eu-governed-agent B3 (PR #40, decision 2). Corpus-first:
these vectors are written and shown **failing against the current runtime** before the route exists.

## What `cgr.disposition.v1` is

The accepted `cgr.cosign.v1` envelope (`docs/cgr/cgr-cosign-v1-spec.md`, decisions 0009/0010/0011)
with `profile = "cgr.disposition.v1"`. **Nothing AML-specific lives here** — `content_body` is a
generic opaque object; the AML *content* profile lives downstream in eu-governed-agent B2 (ADR-0008).

## Model (spec §3.2) — the runtime counter-signs

The **system signature is the issuer's counter-signature**: the capturing runtime (grafomem)
produces it with its **pinned signing identity**, and pins that key id as the **sole trusted issuer**
(operator decision 4). So the `POST /v1/dispositions` body is the **approver-signed inner**
(`content_body` + `approval_assertion` + `approver_signature`, signed by the human approver's
self-custodied key); the runtime verifies it, checks the approver is **enrolled** for the tenant,
counter-signs (system layer), and persists. Trust set (decision 4): the tenant's **enrolled approver
keys** (`hitl_approvers`) + the **runtime's pinned system key id**; **empty registry → reject**; no
env-var trusted list.

## Vectors

| id | title | offline verify | runtime POST | layer |
|----|-------|----------------|--------------|-------|
| D1 | valid two-signature disposition | valid | 201 | both |
| D2 | missing approver signature | invalid | 422 | both |
| D3 | approver key not in tenant registry | valid (crypto) | 403 (`approver_not_enrolled`) | runtime |
| D4 | wrong system key (untrusted issuer) | invalid (`issuer_untrusted`) | — | verify-only |
| D5 | unresolvable predicate (verifier-only) | invalid (`predicate_unresolved`) | — | verify-only |
| D6 | assurance marker surfaced, never gated | valid | 201 (surfaced on read) | both |

D3 self-verifies cryptographically but the **runtime** rejects it (approver not enrolled) — a
runtime-layer check, not a crypto one. D4 is an **offline-verify** vector (under the counter-sign
model the client cannot POST a system key) — it proves the verifier/GET path pins the issuer.

## Layers (`tests/test_disposition_conformance.py`)

1. **`test_corpus_wellformed`** — always runs; every `expect_verify` verdict holds under the
   reference verifier (`packages/grafomem-cgr` cosign_verify) + this fixture registry + the pinned
   trusted issuer. Green before the route exists.
2. **`test_runtime_conformance`** — set `CGR_DISPOSITION_BASE` + `CGR_DISPOSITION_KEY` (a
   `disposition:write` key) to POST each vector and assert `http_expect`. **Fails against the current
   runtime** (route absent) — the target the implementation turns green.

## Profile registry — RATIFIED

The `cgr.disposition.v1` entry (`approval_mode=bound`, `approver_signature=REQUIRED`, required-ness
**unconditional**) is the normative contract at `docs/cgr/cosign-profile-registry.json` (decision:
`cosign-disposition-v1-profile-registry`, accept-by-merge). `registry.json` here **mirrors** it — it is
the contract, not a fixture; the runtime resolves profiles from the normative registry. The separate
`cgr.disposition.v1.predicate` profile is a **verifier-conformance fixture only** (exercises
`predicate_unresolved`, decisions 0010/0011) and is NOT part of the disposition contract.

## Regenerate

```
python3 conformance/cgr-disposition-v1/generate.py   # deterministic; test keys, NOT real keys
```

## Corrections

- **2026-09-19 — D6 `record_nonce` `disp-0001` → `disp-0006`.** D6 (the surfaced-assurance vector)
  is an **independent valid record**, distinct from D1, but the generator's default nonce left both
  on `disp-0001`. Under the ledger-class replay guard `UNIQUE(approver_key_id, record_nonce)`
  (spec §4), posting D1 then D6 to one tenant made D6 a replay of D1's `(approver_key_id,
  record_nonce)` pair. **How found:** the runtime conformance leg returned **HTTP 409** for D6 (it
  expects 201) — the runtime was correct; the corpus was wrong. Fix: two distinct valid dispositions
  carry distinct nonces; D6 now uses `disp-0006`. Offline verdicts unchanged; runtime leg green.
