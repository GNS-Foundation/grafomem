# Claude Code Ticket #5 — CGR Identity-Key Binding (cross-repo: grafomem + geiant)

**Repos:** `~/grafomem` (Part 5a) + `~/geiant` (Part 5b)  ·  **Owner (architect):** Camilo + Cowork-chat (spec)  ·  **You:** implementer
**Bases:** 5a → branch `cgr/identity-binding` off grafomem `main`; 5b → branch `cgr/identity-binding` off geiant `main` (starts only after 5a merges + emits).
**Depends on:** #4a/#4a.1 (issuance + JCS) and #4b (consumption) — all merged. This is the hardening that must land **before any real agent is onboarded**.
**Scope:** Bind a CGR attestation to the agent's **GEIANT identity public key**, not to its `agent_handle` string. Capture that key at decision time (irreversible), carry it through scoring → the Foundation-**signed** attestation body → GEIANT verification. **No scoring-math change beyond the aggregation key; neutrality seam unchanged.**

---

## Context — why this is a real hole (grounded in the code)

Today the whole reputation chain is bound to `agent_handle`, and in GEIANT a handle is **`<facet>@<territory>`** — explicitly *not* a unique identity:

- `packages/core/src/types/index.ts`: `AntIdentity.publicKey` is documented as *"Ed25519 public key — 64 hex chars. **This IS the agent's identity.**"* The `handle` is *"`<facet>@<territory>` … capability scope … H3 jurisdiction."* `buildHandle(facet, territoryName)` derives it purely from facet + territory — so **many distinct keypairs can share one handle** (`finance@swiss-central` is a role, not an agent).
- `packages/core/src/agent/cgr.ts` + `identity.ts`: `cgrBand()` binds via `verifyCGRAttestation(manifest.cgr, key, { expectedHandle: manifest.identity.handle })` — **string equality on the handle**.
- grafomem `src/aml/cgr/substrate.py`: `DecisionRow.agent_handle = p.get("agent_handle")` from decision params JSONB. **No GEIANT public key is captured at decision time.** The attestation signed body (`attestation.py` docstring + `engine.to_tiergate`) carries `agent_handle` and **no key**.

The rest of GEIANT already keys by the actual identity — `DelegationCert.agentPublicKey`, `VirtualBreadcrumb.agentPublicKey`, `SpatialMemoryNode.agentPublicKey`. **CGR is the one subsystem binding reputation to a mutable, non-unique string.** Consequences: an agent can inherit another's CGR by registering the same `facet@territory` handle; a reassigned/rotated handle silently carries reputation to a different keypair; the Foundation signs a claim that names a role, not a principal. Fails safe today only because real agents aren't onboarded yet — this ticket closes it before they are.

**The fix:** make the binding subject the GEIANT identity public key (the thing that *is* the identity), captured at decision time and committed inside the Foundation signature.

## The non-negotiable invariants
1. The attestation's bound subject key (`subject_key`) is the **agent's** GEIANT Ed25519 pubkey — it MUST be distinct from the Foundation `issuer_key_id` (the neutrality key) **and** from grafomem's commercial `signing_identity`. A test asserts `subject_key != issuer_key_id`.
2. The subject key is **captured at decision time**, never back-resolved from `agent_handle → registry` at issuance time. Back-resolution would re-trust the exact handle mapping this ticket exists to stop trusting. If the key wasn't captured, the agent is `unproven` — never guessed.
3. GEIANT binds on the **key**; the handle becomes a human-readable label, not an authority. Key mismatch ⇒ not this agent's reputation ⇒ `unproven`.

---

# PART 5a — grafomem: capture the key + bind the signed body

**Base:** `cgr/identity-binding` off grafomem `main`.

## Read first (real files)
- `src/aml/cgr/substrate.py` — `DecisionRow` (the 10-key export shape), `load_substrate` (`p.get("agent_handle")`), `export_rows` (the byte-for-byte export contract — **guarded by a regression test**, so extend it deliberately).
- `src/aml/cgr/engine.py` — `compute_scores(...)` (aggregation key) + `to_tiergate(...)` (the dict wrapped into the attestation).
- `src/aml/cgr/attestation.py` — `build_attestation` spreads the tiergate dict into the signed body; `CGR_ATTESTATION_SCHEMA`. Adding a key to `to_tiergate` automatically puts it in the signed body.
- `src/aml/cgr/issuance.py` — Foundation identity (unchanged here).

## Task A — capture `agent_key` at decision time (the irreversible field)
- Extend the decision-capture path so a governed decision records the acting agent's **GEIANT Ed25519 public key** (64-hex) in `decision_records.parameters`, key `agent_key`, alongside the existing `agent_handle`. Same channel/discipline as the three irreversible fields in the substrate-instrumentation spec — the **emitter supplies it** (the agent/adapter that already sends `agent_handle`). Do NOT look it up from a registry.
- `substrate.py`: add `agent_key: str | None` to `DecisionRow`; populate from `p.get("agent_key")` in `load_substrate`. Absent ⇒ `None` (legacy/backward-compatible).
- `export_rows`: add `agent_key` to the serialized shape. **Update the export regression test/fixture** in the same commit (this is the guarded contract — an 11th key, appended).

## Task B — aggregate + emit by key
- `engine.compute_scores`: aggregate per agent by **`agent_key` when present, falling back to `agent_handle`** when null (legacy rows). One agent = one key.
- `engine.to_tiergate`: add `subject_key` (the bound GEIANT pubkey hex, or `null` if the agent's decisions carried no key) to the returned dict. Keep `agent_handle` as the human-readable label. Optional: `subject_did` = `did:key` form of the pubkey — **skip for now** unless trivial (raw hex is the authoritative binding; did:key is a later display alias, and a DID lib is out of scope).
- Because `build_attestation` spreads the tiergate dict, `subject_key` lands in the **signed body** automatically — no change to the signing call.

## Task C — schema bump v1 → v2
- `attestation.py`: bump `CGR_ATTESTATION_SCHEMA = "cgr.attestation.v2"` (the signed body changed, so the schema string must change). Keep `verify_attestation` version-agnostic (it re-canonicalizes whatever body it's given).
- An `unproven` cold-start agent still gets a valid signed v2 attestation with `subject_key` set (band `unproven`). An agent whose decisions carried **no** `agent_key` gets `subject_key: null` — honest, and GEIANT will read it as `unproven` (can't bind).

## Tests (5a) — match #1–#4a style
- **Binding invariant:** `subject_key != issuer_key_id` and `subject_key != signing_identity.public_key()`.
- Capture round-trip: a decision with `agent_key` → `DecisionRow.agent_key` set → `to_tiergate.subject_key` set → present in the signed body → survives `build_attestation`/`verify_attestation`.
- Aggregation: two decisions with the **same `agent_key` but different `agent_handle`** aggregate to ONE agent (key wins); two with same handle but different keys are TWO agents.
- Legacy: rows with no `agent_key` → `subject_key: null`, aggregate by handle exactly as today (no score change on the existing fixtures).
- `export_rows` regression updated (11-key shape); schema string is `cgr.attestation.v2`.
- Import-isolation grep on `src/aml/cgr/` still clean; existing suite green.

## Hand-off (5a)
Diff summary; test output incl. the binding-invariant + key-vs-handle aggregation tests; a fresh **golden attestation** built with the fixture seed (`0x11…11`) over a known `to_tiergate` that now includes a fixed `subject_key` — **dump `{attestation, canonical_body_utf8, issuer_key_id}`** exactly like the existing `cgr_attestation_v1_jcs.golden.json`, because 5b needs it as the v2 fixture. 3-line note: how `agent_key` is sourced from the emitter, and confirmation `subject_key` is inside the signed body (not the envelope).

---

# PART 5b — geiant: verify the key binding

**Base:** `cgr/identity-binding` off geiant `main`. Starts after 5a merges and can emit a v2 golden fixture.

## Read first (real files)
- `packages/core/src/types/index.ts` — `CGRAttestation` (schema literal `'cgr.attestation.v1'`), `AntIdentity.publicKey`.
- `packages/core/src/agent/cgr.ts` — `verifyCGRAttestation` (the `expectedHandle` option at line ~104), `canonCGRBody`, the pinned-key checks.
- `packages/core/src/agent/identity.ts` — `cgrBand()` passes `expectedHandle: manifest.identity.handle` (the line to change).
- `packages/core/src/__tests__/fixtures/cgr_attestation_v1_jcs.golden.json` + `__tests__/cgr.test.ts` — the fixture + test style to mirror.

## Task D — types
- `CGRAttestation`: add `subject_key: string` (agent pubkey hex) to the signed-body fields; optional `subject_did?: string`. Change the `schema` literal to `'cgr.attestation.v1' | 'cgr.attestation.v2'` (accept both at the type level; runtime handles the difference).
- `canonCGRBody` needs **no** change — `subject_key` is a body field, not an envelope key, so it's included automatically (this is the point: it's inside the signature).

## Task E — verify + bind on key
- `verifyCGRAttestation`: add `expectedKey?: string` to `VerifyOptions`. New rule: **if the attestation is v2, require `att.subject_key === expectedKey`** (reject with reason `'subject_key does not match manifest identity key'` on mismatch or missing key). Keep `expectedHandle` as an **optional advisory** check only (a warning path, or drop it from the binding — the key is authoritative; the handle is a label).
- `cgrBand()` (identity.ts): pass `{ expectedKey: manifest.identity.publicKey }` instead of `{ expectedHandle: manifest.identity.handle }`. This is the core fix.
- **Fail-safe back-compat:** a **v1** attestation (no `subject_key`) can no longer be bound to a key → `cgrBand()` returns `'unproven'`. That's stricter than today but correct (no real agents onboarded, and v1 binding was the hole). Document it in the function comment; `effectiveTrust`/`scoreAntFitness` already treat `unproven` as today's legacy behavior, so routing degrades safely.
- Keep `issuer_key_id === pinned`, schema, issuer, freshness, and the raw-byte Ed25519 signature checks exactly as they are.

## Task F — golden fixture v2 + tests
- Commit the 5a-produced v2 fixture as `cgr_attestation_v2_jcs.golden.json` (keep v1 alongside for a legacy-rejection test).
- Tests (mirror `cgr.test.ts`):
  - v2 golden verifies **true** with `expectedKey = <the fixture's subject_key>` and the pinned Foundation key; **false** on a one-byte tamper of `subject_key` (signature breaks — proves the key is inside the signed body).
  - Wrong manifest key (attestation's `subject_key` ≠ `manifest.identity.publicKey`) → `verifyCGRAttestation` invalid → `cgrBand` `unproven`.
  - v1 legacy fixture → `cgrBand` `unproven` (fail-safe), not a granted band.
  - Handle differs but key matches → still valid (key is authoritative; handle is a label).
  - `subject_key` present but equal to `issuer_key_id` → treat as invalid (defense in depth — the neutrality invariant, mirrored on the consumer).
  - Existing CGR suite green.

## Hand-off (5b)
Diff summary; the v2 fixture + its provenance (the 5a seed/input that generated it); test output; a 3-line note on: the `expectedKey` binding, the v1→`unproven` fail-safe decision, and confirmation `canonCGRBody` needed no change (subject_key rode inside the signed body).

---

## Acceptance / definition of done (whole ticket)
1. grafomem captures `agent_key` at decision time, aggregates by it, and emits a v2 Foundation-signed attestation whose **signed body** contains `subject_key` (distinct from `issuer_key_id` and the commercial key — tested).
2. GEIANT binds `cgrBand()` on `manifest.identity.publicKey`; a key mismatch or a v1 attestation reads `unproven`; a tampered `subject_key` fails the signature.
3. Legacy paths degrade safely to `unproven`; no scoring-math change beyond the aggregation key; neutrality seam unchanged.
4. Golden v2 fixture is the cross-repo contract; new + existing tests green in both repos.

## Non-goals (explicit)
- **Key rotation / identity continuity** across a keypair change (reputation following an identity through rotation) — needs an identity graph / DID document with rotation proofs. Documented as the next hardening after this; NOT in scope.
- No `did:key` library dependency now (raw hex is the binding; did:key is a later display alias).
- No change to the Foundation seam, the JCS canonicalization, or the volume-tier ladder.
- No back-resolution of key from handle (explicitly forbidden — it reintroduces the vulnerability).

## Merge order
5a merges to grafomem `main` and emits the v2 fixture → 5b consumes it and merges to geiant `main`. Camilo brings each diff to the Cowork chat for review before merge (same loop as #4a→#4b). *(Note: "#5" here is the CGR ticket number; unrelated to grafomem GitHub PR #5, which was the `validate_real` reference.)*
