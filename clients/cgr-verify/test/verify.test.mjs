// Offline verifier tests — run: npm test
// Proves: (1) JS JCS canonicalization is byte-identical to grafomem's rfc8785 (the
// signature made over Python's canonical bytes verifies under JS's), (2) a valid
// attestation verifies against the pinned key, (3) any single-field tamper fails —
// including the SIGNED scope fields, (4) v2 (legacy) still verifies, (5) binding works.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { verifyCGRAttestation, canonCGRBody } from '../src/index.js';

const here = dirname(fileURLToPath(import.meta.url));
const load = (f) => JSON.parse(readFileSync(join(here, '..', 'fixtures', f), 'utf-8'));
const v3 = load('cgr_attestation_v3_jcs.golden.json');
const v2 = load('cgr_attestation_v2_jcs.golden.json');

let passed = 0;
const ok = (name, cond) => { assert.ok(cond, name); console.log('  ✓', name); passed++; };

// 1. CROSS-LANGUAGE PARITY: JS canonicalize() == Python rfc8785 (the committed golden bytes)
ok('JCS parity: canonCGRBody bytes == golden canonical_body_utf8 (rfc8785)',
   new TextDecoder().decode(canonCGRBody(v3.attestation)) === v3.canonical_body_utf8);

const PIN = v3.issuer_key_id;

// 2. valid attestation verifies + returns score inseparable from evidence + scope
const r = await verifyCGRAttestation(v3.attestation, PIN);
ok('v3 valid', r.valid === true);
ok('v3 schema', r.schema === 'cgr.attestation.v3');
ok('score + both masses returned', r.score !== undefined && r.evidenceMass !== undefined && r.nResolved !== undefined);
ok('scoring_scope pooled (not per-domain)', r.scoringScope === 'pooled');
ok('freshness present', typeof r.lastResolvedAt === 'string');

// 3. tamper every field — including SIGNED scope fields — must fail
for (const [k, v] of Object.entries({
  cgr_score: 0.99, last_resolved_at: '2020-01-01T00:00:00Z',
  scoring_scope: 'domain-specific', requested_domain: 'security-scan',
  domain_n_resolved: 999, subject_key: '00'.repeat(32), n_resolved: 999,
})) {
  const t = await verifyCGRAttestation({ ...v3.attestation, [k]: v }, PIN);
  ok(`tamper ${k} → invalid`, t.valid === false);
}

// 4. wrong pinned key fails; missing pin fails closed
ok('wrong pinned key → invalid', (await verifyCGRAttestation(v3.attestation, '22'.repeat(32))).valid === false);
ok('no pinned key → invalid (fail closed)', (await verifyCGRAttestation(v3.attestation, '')).valid === false);

// 5. legacy v2 still verifies (backward compat)
ok('v2 legacy verifies', (await verifyCGRAttestation(v2.attestation, v2.issuer_key_id)).valid === true);

// 6. identity binding
ok('expectedKey match → valid', (await verifyCGRAttestation(v3.attestation, PIN, { expectedKey: v3.subject_key })).valid === true); // gitleaks:allow (public key var, not a secret)
ok('expectedKey mismatch → invalid', (await verifyCGRAttestation(v3.attestation, PIN, { expectedKey: '11'.repeat(32) })).valid === false);

console.log(`\n${passed} checks passed`);
