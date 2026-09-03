// SMOKE TEST for cgr.attestation.v4 — run: npm test
//
// Proves the SHIPPED code runs and the headline v4 paths work end to end:
//   1. non-enforcing mode (clean subject verifies)
//   2. enforcing mode with a seek that finds nothing (still valid)
//   3. a HELD `revokes` edge targeting the subject → valid:false
//   4. a `continues` edge → lineage_status
//   5. `seek` that THROWS in enforcing mode → fails closed (Validity-Fails-Closed)
//   6. `mode` is required — omitting it throws (no silent default)
//
// Self-contained and FIXTURE-FREE: mints attestations in-test from a known seed (no golden files),
// so it runs from an installed package as well as from the repo.
//
// ── THIS IS A SMOKE TEST, NOT THE CONTRACT ──────────────────────────────────────────────────
// It checks that the code runs and the headline paths behave; it is NOT exhaustive. The
// authoritative, exhaustive definition of v4 verification is the 38-vector conformance corpus at
// conformance/cgr-attestation-v4/ (both modes). If this file and the corpus ever disagree, the
// CORPUS WINS. Do not grow this into a second corpus, and do not mistake it for the spec.
// ────────────────────────────────────────────────────────────────────────────────────────────

import assert from 'node:assert/strict';
import * as ed from '@noble/ed25519';
import { verifyCGRAttestationV4, canonCGRBody, attestationFingerprint } from '../src/index.js';

let passed = 0;
const ok = (name, cond) => { assert.ok(cond, name); console.log('  ✓', name); passed++; };
const hex = (b) => Buffer.from(b).toString('hex');

// Known seed → deterministic Foundation issuer key. TEST-ONLY; not a real key.
const ISSUER_PRIV = new Uint8Array(32).fill(1);
const ISSUER_PUB = hex(await ed.getPublicKeyAsync(ISSUER_PRIV));
const SUBJECT_KEY = 'ab'.repeat(32); // distinct from the issuer (neutrality invariant)

// Mint a Foundation-signed v4 attestation from a partial body (Ed25519 over the JCS signed body).
async function mint(extra = {}) {
  const body = {
    schema: 'cgr.attestation.v4',
    issuer: 'gns-foundation',
    issuer_key_id: ISSUER_PUB,
    subject_key: SUBJECT_KEY,
    subject_did: 'did:key:zSmokeTest',
    agent_handle: 'smoke@test',
    dimension: 'receivables', // non-grounding → oracle_id/audit_policy MUST be absent
    cgr_score: 0.5,
    confidence: 4.0,
    n_resolved: 8,
    capability_tier: 0.5,
    as_of: '2026-01-01T00:00:00Z',
    last_resolved_at: '2026-01-01T00:00:00Z',
    scoring_scope: 'pooled',
    requested_domain: null,
    domain_n_resolved: null,
    rationale: 'smoke',
    // NOTE: no `domain` / `decision_date` / `backfilled` — this is a pooled judgment aggregate
    // (scoring_scope: 'pooled'), and the §2.2 pooled-aggregate absence gate requires those three
    // PER-RECORD fields to be ABSENT. recorded_at + verifiability_tag are universal and stay.
    verifiability_tag: 'judgment',
    recorded_at: '2026-01-01',
    ...extra,
  };
  const signature = hex(await ed.signAsync(canonCGRBody(body), ISSUER_PRIV));
  return { ...body, signature, evidence_ref: null };
}

const subject = await mint();

// 1. non-enforcing: a clean subject verifies.
const rn = await verifyCGRAttestationV4(subject, {}, ISSUER_PUB, { mode: 'non-enforcing' });
ok('non-enforcing: clean subject valid', rn.valid === true);
ok('non-enforcing: schema echoed', rn.schema === 'cgr.attestation.v4');

// 2. enforcing with a seek that finds nothing: still valid.
const re = await verifyCGRAttestationV4(subject, {}, ISSUER_PUB, { mode: 'enforcing', seek: async () => [] });
ok('enforcing: no edges found → valid', re.valid === true);

// 3. a HELD revokes edge targeting the subject → valid:false, reason distinct from a bad signature.
const subjFp = attestationFingerprint(subject);
const revokeEdge = await mint({
  subject_key: 'cd'.repeat(32),
  relates_to: [{ type: 'revokes', target: { kind: 'attestation', hash_alg: 'blake2b-256', hash: subjFp } }],
});
const rr = await verifyCGRAttestationV4(subject, {}, ISSUER_PUB, { mode: 'non-enforcing', heldEdges: [revokeEdge] });
ok('held revokes → invalid', rr.valid === false);
ok('held revokes → reason mentions revoked', /revoked/i.test(rr.reason || ''));

// 4. a continues edge (with the REQUIRED evidence_tier) whose predecessor is in the ledger.
const predecessor = await mint({ subject_key: 'ef'.repeat(32) });
const predFp = attestationFingerprint(predecessor);
const predTarget = { kind: 'attestation', hash_alg: 'blake2b-256', hash: predFp };
const withLineage = await mint({
  relates_to: [{ type: 'continues', target: predTarget, evidence_tier: 'custody_record' }],
});
const rc = await verifyCGRAttestationV4(
  withLineage, { attestations: { [predFp]: predecessor } }, ISSUER_PUB, { mode: 'non-enforcing' },
);
ok('continues (resolved) → valid', rc.valid === true);
ok('continues (resolved) → lineage_status complete', rc.lineage_status === 'complete');
ok('continues → evidence_tier surfaced (not gated)', rc.evidence_tier === 'custody_record');

// 4b. a continues edge WITHOUT evidence_tier → reject (§1.1 required-on-continues).
const noTier = await mint({ relates_to: [{ type: 'continues', target: predTarget }] });
const rnt = await verifyCGRAttestationV4(noTier, { attestations: { [predFp]: predecessor } }, ISSUER_PUB, { mode: 'non-enforcing' });
ok('continues missing evidence_tier → invalid', rnt.valid === false);
ok('missing evidence_tier → reason mentions evidence_tier', /evidence_tier/i.test(rnt.reason || ''));

// 5. seek THROWS in enforcing mode → fail closed.
const rf = await verifyCGRAttestationV4(subject, {}, ISSUER_PUB, {
  mode: 'enforcing', seek: async () => { throw new Error('store down'); },
});
ok('seek throws → invalid (fail closed)', rf.valid === false);
ok('seek throws → "revocation status undeterminable"', /revocation status undeterminable/i.test(rf.reason || ''));

// 6. §2.2 pooled-aggregate absence gate — a pooled aggregate carrying a per-record field → reject.
const withDomain = await mint({ domain: 'deploy' }); // scoring_scope is 'pooled'
const rd = await verifyCGRAttestationV4(withDomain, {}, ISSUER_PUB, { mode: 'non-enforcing' });
ok('domain on pooled aggregate → invalid', rd.valid === false);
ok('absence gate → reason mentions domain', /domain/i.test(rd.reason || ''));

const withDecisionDate = await mint({ decision_date: '2026-01-01' }); // pooled → must be absent
const rdd = await verifyCGRAttestationV4(withDecisionDate, {}, ISSUER_PUB, { mode: 'non-enforcing' });
ok('decision_date on pooled aggregate → invalid', rdd.valid === false);
ok('absence gate → reason mentions decision_date', /decision_date/i.test(rdd.reason || ''));

// 7. §2.2 universal presence gate — a record missing verifiability_tag → reject.
const noTag = await mint({ verifiability_tag: undefined });
const rvt = await verifyCGRAttestationV4(noTag, {}, ISSUER_PUB, { mode: 'non-enforcing' });
ok('missing verifiability_tag → invalid', rvt.valid === false);
ok('presence gate → reason mentions verifiability_tag', /verifiability_tag/i.test(rvt.reason || ''));

// 6. mode is required — omitting it throws (no silent default).
let threw = false;
try { await verifyCGRAttestationV4(subject, {}, ISSUER_PUB, {}); } catch (e) { threw = e instanceof TypeError; }
ok('missing mode → TypeError (no silent default)', threw);

console.log(`\n${passed} v4 smoke checks passed`);
