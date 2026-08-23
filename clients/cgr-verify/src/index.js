// @gns-foundation/cgr-verify — offline verifier for CGR attestations.
//
// Verify a Foundation-signed CGR attestation against a PINNED issuer public key,
// with no dependency on any grafomem server. The recipe is deliberately simple and
// language-agnostic (see README): canonicalize the signed body (RFC 8785 / JCS),
// then Ed25519-verify the signature over the RAW canonical bytes (NO SHA-512 prehash)
// under the pinned key. Two footguns this pins for you:
//   1. envelope keys `signature` and `evidence_ref` are EXCLUDED from the signed body;
//   2. Ed25519 is over the raw canonical bytes — do not pre-hash.
import canonicalize from 'canonicalize';
import * as ed from '@noble/ed25519';

export const CGR_ISSUER = 'gns-foundation';
export const ACCEPTED_SCHEMAS = new Set([
  'cgr.attestation.v1', 'cgr.attestation.v2', 'cgr.attestation.v3',
]);
const ENVELOPE_KEYS = new Set(['signature', 'evidence_ref']);

/** Canonical (JCS) bytes of the signed body — everything except the envelope keys. */
export function canonCGRBody(att) {
  const body = {};
  for (const [k, v] of Object.entries(att)) {
    if (!ENVELOPE_KEYS.has(k)) body[k] = v;
  }
  return new TextEncoder().encode(canonicalize(body));
}

/**
 * Verify a CGR attestation offline against a pinned Foundation public key (hex).
 * Returns { valid, reason?, ...fields } — score/evidence are only returned on success,
 * read from the now-verified body (never trust an unverified attestation's fields).
 *
 * opts.expectedKey  — if set, the attestation's subject_key MUST equal it (identity binding).
 * opts.maxAgeMs / opts.nowMs — optional freshness gate on last_resolved_at.
 */
export async function verifyCGRAttestation(att, pinnedIssuerPubKeyHex, opts = {}) {
  if (!att || typeof att !== 'object') return { valid: false, reason: 'no attestation' };
  if (!pinnedIssuerPubKeyHex) return { valid: false, reason: 'no pinned issuer key (fail closed)' };
  if (!ACCEPTED_SCHEMAS.has(att.schema)) return { valid: false, reason: `unsupported schema: ${att.schema}` };
  if (att.issuer !== CGR_ISSUER) return { valid: false, reason: `issuer mismatch: ${att.issuer}` };
  if (att.issuer_key_id !== pinnedIssuerPubKeyHex) {
    return { valid: false, reason: 'issuer_key_id does not equal the pinned key' };
  }
  // neutrality invariant: the subject can never be the issuer
  if (typeof att.subject_key === 'string' && att.subject_key === att.issuer_key_id) {
    return { valid: false, reason: 'subject_key equals issuer_key_id (neutrality violation)' };
  }
  if (opts.expectedKey !== undefined) {
    if (typeof att.subject_key !== 'string' || att.subject_key.length === 0) {
      return { valid: false, reason: 'attestation has no subject_key to bind' };
    }
    if (att.subject_key !== opts.expectedKey) {
      return { valid: false, reason: 'subject_key does not match expected identity' };
    }
  }
  if (typeof att.signature !== 'string' || att.signature.length !== 128) {
    return { valid: false, reason: 'signature must be 128 hex chars (64 bytes)' };
  }
  let ok = false;
  try {
    ok = await ed.verifyAsync(att.signature, canonCGRBody(att), pinnedIssuerPubKeyHex);
  } catch (e) {
    return { valid: false, reason: `verify error: ${e && e.message ? e.message : e}` };
  }
  if (!ok) return { valid: false, reason: 'signature verification failed' };

  // optional freshness gate (advisory; the signed fact is last_resolved_at itself)
  if (opts.maxAgeMs !== undefined && att.last_resolved_at) {
    const now = opts.nowMs ?? Date.now();
    const age = now - Date.parse(att.last_resolved_at);
    if (Number.isFinite(age) && age > opts.maxAgeMs) {
      return { valid: false, reason: 'attestation older than maxAgeMs' };
    }
  }
  return {
    valid: true,
    subjectKey: att.subject_key,
    subjectDid: att.subject_did,
    dimension: att.dimension,
    score: att.cgr_score,
    evidenceMass: att.confidence,     // pooled n = α+β (backs the score)
    nResolved: att.n_resolved,
    scoringScope: att.scoring_scope,  // "pooled" — NOT a per-domain score
    requestedDomain: att.requested_domain,
    domainNResolved: att.domain_n_resolved,  // backs the domain match
    lastResolvedAt: att.last_resolved_at,
    schema: att.schema,
  };
}
