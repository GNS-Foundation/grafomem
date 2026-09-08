// ═══════════════════════════════════════════════════════════════════════════
// cgr.cosign.v1 — two-party co-signature envelope verifier.
// Implements docs/cgr/cgr-cosign-v1-spec.md §8 (amended by decision 0010:
// predicate-unresolved -> REJECT). Target: conformance/cgr-cosign-v1/vectors.json.
//
// Canonicalization + signatures are IDENTICAL to the attestation verifier: RFC 8785
// (JCS), Ed25519 over the raw canonical bytes with NO prehash. BLAKE2b-256 content
// digests (§2.1). The system signature is over the record MINUS the envelope keys,
// which INCLUDES approver_signature (§2.3) — that is what makes stripping detectable.
// ═══════════════════════════════════════════════════════════════════════════
import canonicalize from 'canonicalize';
import * as ed from '@noble/ed25519';
import { blake2b } from '@noble/hashes/blake2b';
import { bytesToHex } from '@noble/hashes/utils';

export const COSIGN_SCHEMA = 'cgr.cosign.v1';
export const DOMAIN_TAG = 'grafomem.hitl.approval.v1:';                 // §9
const ENVELOPE_KEYS = new Set(['system_signature', 'evidence_ref']);   // §2.3
const OPS = new Set(['eq', 'ne', 'in', 'lt', 'lte', 'gt', 'gte']);
const ORDER_OPS = new Set(['lt', 'lte', 'gt', 'gte']);

const fail = (reason, extra = {}) => ({ valid: false, reason, ...extra });
const jcs = (obj) => new TextEncoder().encode(canonicalize(obj));
const stripPrefix = (s) => (typeof s === 'string' && s.includes(':') ? s.split(':').slice(-1)[0] : s);

/** §2.1 content_digest of a content_body. */
export function contentDigest(body) {
  return 'b2-256:' + bytesToHex(blake2b(jcs(body), { dkLen: 32 }));
}

function systemBodyBytes(record) {
  const body = {};
  for (const [k, v] of Object.entries(record)) if (!ENVELOPE_KEYS.has(k)) body[k] = v;
  return jcs(body);
}

function resolvePath(obj, path) {
  let cur = obj;
  for (const seg of String(path).split('.')) {
    if (cur === null || typeof cur !== 'object' || Array.isArray(cur) || !(seg in cur)) {
      return { found: false };
    }
    cur = cur[seg];
  }
  return { found: true, value: cur };
}

const isScalar = (v) => v !== null && (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean');

/** §5.1 predicate over content_body. Returns {resolved, holds}. `resolved:false` is the
 *  UNDETERMINED case (absent/null/non-scalar field, or a type-mismatched ordering operand)
 *  — decision 0010 makes it a REJECT, handled by the caller. */
function evalPredicate(pred, body) {
  if (!pred || typeof pred !== 'object' || !('field' in pred) || !OPS.has(pred.op)) {
    return { resolved: false };
  }
  const r = resolvePath(body, pred.field);
  if (!r.found || !isScalar(r.value)) return { resolved: false };
  const v = r.value, val = pred.value;
  switch (pred.op) {
    case 'eq': return { resolved: true, holds: v === val };
    case 'ne': return { resolved: true, holds: v !== val };
    case 'in': return { resolved: true, holds: Array.isArray(val) && val.includes(v) };
    default: {                                          // ordering ops: numbers only
      if (typeof v !== 'number' || typeof val !== 'number') return { resolved: false };
      const c = { lt: v < val, lte: v <= val, gt: v > val, gte: v >= val };
      return { resolved: true, holds: c[pred.op] };
    }
  }
}

const isB2 = (h) => typeof h === 'string' && /^b2-256:[0-9a-f]{64}$/.test(h);

/**
 * Verify a cgr.cosign.v1 record. §8 ordered checks, failing closed on the first failure.
 *   record   — the envelope.
 *   registry — { profiles: { <profile>: {approval_mode, approver_signature, referenced_records} } }.
 *   ledger   — { seen: [[approver_key_id, record_nonce], ...], resolvable: [<b2-256 hash>...] }.
 * Returns { valid, reason?, surfaced?, references_unresolved? }. The assurance tier is
 * SURFACED, never gated on (§8).
 */
export async function verifyCosign(record, registry, ledger = {}) {
  if (!record || typeof record !== 'object') return fail('no record');

  // 1. schema
  if (record.schema !== COSIGN_SCHEMA) return fail(`unsupported schema: ${record.schema}`);

  // 2. profile resolution
  const entry = registry && registry.profiles && registry.profiles[record.profile];
  if (!entry) return fail(`unknown profile: ${record.profile}`);
  if (record.approval_mode !== entry.approval_mode) {
    return fail(`approval_mode mismatch: record ${record.approval_mode} != profile ${entry.approval_mode}`);
  }

  const a = record.approval_assertion;
  const body = record.content_body;

  // 3. content integrity (only meaningful when an approver assertion carries a digest)
  if (a && typeof a === 'object') {
    if (a.content_digest !== contentDigest(body)) return fail('content_digest mismatch');
  }

  // 3a. required-ness resolution (§5.1 / decision 0010)
  const req = entry.approver_signature;
  let required;
  if (req === 'REQUIRED') {
    required = true;
  } else if (req && typeof req === 'object' && req.required_when) {
    const e = evalPredicate(req.required_when, body);
    if (!e.resolved) return fail('predicate_unresolved');        // 0010: UNDETERMINED -> reject
    required = e.holds;
  } else {
    return fail('profile malformed: no required-ness declared');
  }

  // 4. approver signature
  const present = typeof record.approver_signature === 'string' && record.approver_signature.length > 0;
  if (required && !present) return fail('approver signature required but absent');
  if (present) {
    let ok = false;
    try {
      const msg = new Uint8Array([...new TextEncoder().encode(DOMAIN_TAG), ...jcs(a)]);
      ok = await ed.verifyAsync(stripPrefix(record.approver_signature), msg, stripPrefix(a.approver_key_id));
    } catch (e) { return fail(`approver signature verify error: ${e && e.message}`); }
    if (!ok) return fail('approver signature invalid');
  }

  // 5. approval binds this content (explicit; the property that matters)
  if (a && a.content_digest !== contentDigest(body)) return fail('approval does not bind this content');

  // 6. system signature — over a body INCLUDING approver_signature; catches stripping (§7.1)
  {
    let ok = false;
    try {
      ok = await ed.verifyAsync(stripPrefix(record.system_signature),
                                systemBodyBytes(record),
                                stripPrefix(record.system_metadata.issuer_key_id));
    } catch (e) { return fail(`system signature verify error: ${e && e.message}`); }
    if (!ok) return fail('system signature verification failed');
  }

  // 7. nonce uniqueness (§4)
  if (a) {
    const seen = (ledger && ledger.seen) || [];
    if (seen.some(([k, n]) => k === a.approver_key_id && n === a.record_nonce)) {
      return fail('record_nonce replay: duplicate (approver_key_id, record_nonce)');
    }
  }

  // 8. act / draft consistency (§6)
  if (a) {
    const act = a.approver_act;
    if (!['approve', 'modify', 'override'].includes(act)) return fail(`unknown approver_act: ${act}`);
    const hasDraft = a.agent_draft_digest !== undefined;
    if (act === 'approve' && hasDraft) return fail('agent_draft_digest must be absent for approve');
    if ((act === 'modify' || act === 'override') && !hasDraft) {
      return fail(`agent_draft_digest required for ${act}`);
    }
  }

  // 9. free-mode references (§8.9): well-formed required; unresolvable -> degrade (surface)
  let referencesUnresolved = false;
  if (record.approval_mode === 'free') {
    const refs = (body && Array.isArray(body.references)) ? body.references : [];
    for (const r of refs) if (!isB2(r)) return fail(`malformed reference hash: ${r}`);
    const resolvable = new Set((ledger && ledger.resolvable) || []);
    if (refs.some((r) => !resolvable.has(r))) referencesUnresolved = true;   // §8.9 proposed degrade
  }

  // surfaced — MUST surface, MUST NOT gate (§8)
  const out = { valid: true };
  if (a) {
    out.surfaced = {
      approver_id: a.approver_id,
      approver_act: a.approver_act,
      decision_date: a.decision_date,
      assurance_tier: null,          // established by the separate assurance layer (§9/§10, gap 3a)
    };
  }
  if (referencesUnresolved) out.references_unresolved = true;
  return out;
}
