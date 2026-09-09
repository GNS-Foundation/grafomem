// SMOKE TEST for cgr.cosign.v1 — run: npm test
//
// Proves the SHIPPED co-signature verifier runs and the headline §8 paths work end to end:
//   1. a valid two-signature record verifies
//   2. a STRIPPED approver signature fails (nesting: the system sig covered it — §7.1/§8.6)
//   3. THE REQUIRED-INPUT CONTRACT (decision 0011 §8.2a): omitted trusted set → reject;
//      empty trusted set → reject (the fail-open that must never regress)
//   4. ISSUER PINNING (§8.2a): a record signed CORRECTLY by an UNTRUSTED key → reject
//      issuer_untrusted, while the SAME record under a trusted key verifies
//
// Self-contained and FIXTURE-FREE: mints co-signed records in-test from known seeds (issuer 0x11,
// approver 0x22, untrusted issuer 0x33), so it runs from an INSTALLED package as well as the repo.
//
// Why (3) and (4) are load-bearing: without them this smoke test would pass against a verifier that
// NEVER implemented step 2a — it would verify the self-declared issuer and call it valid. (4) mints
// a record that is cryptographically perfect under 0x33 (so a 2a-skipping verifier accepts it) and
// asserts it is rejected unless 0x33 is trusted. That is the N1-class non-vacuity property for the
// issuer gate — the same trap the corpus pins with I1/I2.
//
// ── THIS IS A SMOKE TEST, NOT THE CONTRACT ──────────────────────────────────────────────────
// The authoritative, exhaustive definition is the 28-vector conformance corpus at
// conformance/cgr-cosign-v1/ (decisions 0010 + 0011). If this file and the corpus ever disagree,
// the CORPUS WINS. Do not grow this into a second corpus.
// ────────────────────────────────────────────────────────────────────────────────────────────

import assert from 'node:assert/strict';
import * as ed from '@noble/ed25519';
import canonicalize from 'canonicalize';
import { verifyCosign, contentDigest, DOMAIN_TAG, COSIGN_SCHEMA } from '../src/index.js';

let passed = 0;
const ok = (name, cond) => { assert.ok(cond, name); console.log('  ✓', name); passed++; };
const hex = (b) => Buffer.from(b).toString('hex');
const jcs = (o) => new TextEncoder().encode(canonicalize(o));

// Deterministic TEST keys (repeating-byte seeds; NOT real keys) — match the corpus.
const ISSUER_PRIV = new Uint8Array(32).fill(0x11);
const APPROVER_PRIV = new Uint8Array(32).fill(0x22);
const UNTRUSTED_PRIV = new Uint8Array(32).fill(0x33);
const ISSUER_PUB = hex(await ed.getPublicKeyAsync(ISSUER_PRIV));
const APPROVER_PUB = hex(await ed.getPublicKeyAsync(APPROVER_PRIV));
const UNTRUSTED_PUB = hex(await ed.getPublicKeyAsync(UNTRUSTED_PRIV));
const kid = (pub) => 'ed25519:' + pub;

const ENVELOPE_KEYS = new Set(['system_signature', 'evidence_ref']);

// A minimal working registry — mirrors the shape the README documents.
const REGISTRY = {
  profiles: {
    'p.req': { approval_mode: 'bound', approver_signature: 'REQUIRED', referenced_records: 'none' },
    // predicate profile: approver required only when risk_class == high (optional otherwise)
    'p.pred': {
      approval_mode: 'bound',
      approver_signature: { required_when: { field: 'risk_class', op: 'eq', value: 'high' } },
      referenced_records: 'none',
    },
  },
};

async function approverSign(assertion) {
  // §2.2 — Ed25519 over DOMAIN_TAG ‖ JCS(assertion), no prehash.
  const msg = new Uint8Array([...new TextEncoder().encode(DOMAIN_TAG), ...jcs(assertion)]);
  return 'ed25519-sig:' + hex(await ed.signAsync(msg, APPROVER_PRIV));
}

async function systemSign(record, priv) {
  // §2.3 — Ed25519 over JCS(record minus envelope keys), which INCLUDES approver_signature.
  const body = {};
  for (const [k, v] of Object.entries(record)) if (!ENVELOPE_KEYS.has(k)) body[k] = v;
  return 'ed25519-sig:' + hex(await ed.signAsync(jcs(body), priv));
}

// Mint a co-signed record. `issuerPriv`/`issuerPub` select the system issuer (0x11 normally, 0x33
// for the untrusted-issuer case — the record always self-verifies under its own declared key).
async function mint({ profile = 'p.req', body = { decision: 'file', risk_class: 'high', case_id: 'c-1' },
                      includeApprover = true, issuerPriv = ISSUER_PRIV, issuerPub = ISSUER_PUB } = {}) {
  const rec = { schema: COSIGN_SCHEMA, profile, approval_mode: 'bound', content_body: body };
  if (includeApprover) {
    const assertion = {
      content_digest: contentDigest(body),
      approver_id: 'did:person:test-approver',
      approver_key_id: kid(APPROVER_PUB),
      approver_act: 'approve',
      decision_date: '2026-09-09T00:00:00Z',
      record_nonce: 'nonce-0001',
    };
    rec.approval_assertion = assertion;
    rec.approver_signature = await approverSign(assertion);
  }
  rec.system_metadata = { issuer: 'gns-foundation', issuer_key_id: kid(issuerPub), recorded_at: '2026-09-09' };
  rec.system_signature = await systemSign(rec, issuerPriv);
  rec.evidence_ref = null;
  return rec;
}

const TRUSTED = [kid(ISSUER_PUB)];   // the trusted issuer set the consumer supplies

// 1 — a valid two-signature record verifies.
{
  const rec = await mint();
  const res = await verifyCosign(rec, REGISTRY, {}, TRUSTED);
  ok('valid co-signed record → valid', res.valid === true);
  ok('valid record surfaces approver_act (not gated)', res.surfaced && res.surfaced.approver_act === 'approve');
}

// 2 — stripping the approver signature fails. Under the OPTIONAL (predicate-false) profile the
//     §8.4 required-absent check passes, so the rejection is forced through §8.6 — proving the
//     system signature covered the approver signature (nesting, not parallel signatures).
{
  const signed = await mint({ profile: 'p.pred', body: { decision: 'file', risk_class: 'low', case_id: 'c-2' } });
  const stripped = { ...signed };
  delete stripped.approver_signature;
  const res = await verifyCosign(stripped, REGISTRY, {}, TRUSTED);
  ok('stripped approver signature → reject', res.valid === false);
  ok('stripped → system signature fails (nesting, not §8.4)', /system signature/.test(res.reason || ''));
}

// 3 — the REQUIRED trusted-issuer contract (decision 0011): no default, no trust-everything path.
{
  const rec = await mint();
  const omitted = await verifyCosign(rec, REGISTRY, {});           // 4th arg undefined
  ok('omitted trusted set → reject', omitted.valid === false);
  ok('omitted → no-trusted-set reason', /trusted issuer set/.test(omitted.reason || ''));
  const empty = await verifyCosign(rec, REGISTRY, {}, []);          // empty set
  ok('empty trusted set → reject', empty.valid === false);
  ok('empty → no fallback to self-declared issuer', /trusted issuer set/.test(empty.reason || ''));
}

// 4 — issuer pinning (§8.2a), the N1-class non-vacuity property. The record is signed CORRECTLY by
//     the untrusted 0x33 key and self-declares 0x33, so it verifies cleanly under its own issuer:
//     a verifier that skips step 2a PASSES it. It must be rejected unless 0x33 is trusted.
{
  const rec = await mint({ issuerPriv: UNTRUSTED_PRIV, issuerPub: UNTRUSTED_PUB });
  const untrusted = await verifyCosign(rec, REGISTRY, {}, TRUSTED);          // trusts only 0x11
  ok('untrusted issuer (signed by 0x33) → reject', untrusted.valid === false);
  ok('untrusted issuer → reason is issuer_untrusted', /issuer_untrusted/.test(untrusted.reason || ''));
  const trusted33 = await verifyCosign(rec, REGISTRY, {}, [kid(UNTRUSTED_PUB)]);  // same record, 0x33 trusted
  ok('same record with its issuer trusted → valid (proves the reject was the trust check)', trusted33.valid === true);
}

console.log(`\n${passed} cosign smoke checks passed`);
