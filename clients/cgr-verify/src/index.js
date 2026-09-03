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
import { blake2b } from '@noble/hashes/blake2b';
import { bytesToHex } from '@noble/hashes/utils';

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

// ═══════════════════════════════════════════════════════════════════════════
// cgr.attestation.v4 — relation edges, traversal, grounding gate, held edges.
// See docs/cgr/cgr-attestation-v4-spec.md and conformance/cgr-attestation-v4/.
// v3 verification above is UNCHANGED; v4 is a separate entry point.
// ═══════════════════════════════════════════════════════════════════════════

export const V4_SCHEMA = 'cgr.attestation.v4';
export const GROUNDING_DIMENSIONS = new Set(['grounding']);   // §2.2 (pinned)
const REL_TYPES = new Set(['continues', 'supersedes', 'revokes']);
const VALIDITY_TYPES = new Set(['supersedes', 'revokes']);
const HASH_ALG_FOR_KIND = { attestation: 'blake2b-256', delegation_cert: 'sha-256' };
// §1.1 evidence_tier — closed vocabulary; REQUIRED on continues, MUST be absent on supersedes/revokes.
const EVIDENCE_TIERS = new Set(['custody_record', 'issuer_records', 'operator_verification']);
const HEX64 = /^[0-9a-f]{64}$/;
const MAX_DEPTH = 64;                                          // §1.3 (the pinned floor)

/** BLAKE2b-256 fingerprint of an attestation's canonical signed body (§1.1). */
export function attestationFingerprint(att) {
  return bytesToHex(blake2b(canonCGRBody(att), { dkLen: 32 }));
}

async function _sigOk(att, pinnedIssuerHex) {
  if (typeof att.signature !== 'string' || att.signature.length !== 128) return false;
  try { return await ed.verifyAsync(att.signature, canonCGRBody(att), pinnedIssuerHex); }
  catch { return false; }
}

function _resolve(target, ledger) {
  const map = target.kind === 'attestation'
    ? (ledger && ledger.attestations) || {}
    : (ledger && ledger.delegation_certs) || {};
  return Object.prototype.hasOwnProperty.call(map, target.hash) ? map[target.hash] : null;
}

// Cross-type DFS over the subject's relates_to edges (§1.3), applying the governing
// principle: lineage-only (continues) degrades; validity-affecting (supersedes/revokes)
// fails closed. Returns { reject?: reason, lineage_status?: state }.
function _traverse(subject, ledger) {
  const res = { reject: null, lineage_status: null };
  let sawContinues = false;
  function dfs(node, pathKeys, pathTypes, depth) {
    const edges = Array.isArray(node.relates_to) ? node.relates_to : [];
    for (const e of edges) {
      if (res.reject) return;
      const typ = e.type;
      const key = e.target.kind + ':' + e.target.hash;
      const idx = pathKeys.indexOf(key);
      if (idx !== -1) {                                   // cycle
        const loop = pathTypes.slice(idx).concat([typ]);
        if (loop.some((t) => VALIDITY_TYPES.has(t)))
          res.reject = 'supersedes/revokes chain contains a cycle';
        else
          res.lineage_status = 'anomaly_cycle';
        return;
      }
      if (depth + 1 > MAX_DEPTH) {                        // depth bound
        const path = pathTypes.concat([typ]);
        if (path.some((t) => VALIDITY_TYPES.has(t)))
          res.reject = 'chain exceeds the traversal depth bound';
        else
          res.lineage_status = 'truncated_depth';
        return;
      }
      if (typ === 'continues') sawContinues = true;
      const target = _resolve(e.target, ledger);
      if (target === null) {                              // unreachable target
        if (typ === 'continues' && !res.lineage_status)
          res.lineage_status = 'truncated_unavailable';
        continue;                                         // supersedes/revokes unreachable → no effect
      }
      dfs(target, pathKeys.concat([key]), pathTypes.concat([typ]), depth + 1);
      if (res.reject) return;
    }
  }
  dfs(subject, [], [], 0);
  if (!res.reject && sawContinues && !res.lineage_status) res.lineage_status = 'complete';
  return res;
}

/**
 * Verify a cgr.attestation.v4 attestation offline. ASYNC in BOTH modes (so a caller can write
 * mode-agnostic code). Mode is explicit at the call site — no silent default either way (0006).
 * @param subject       the attestation under evaluation
 * @param ledger        { attestations, delegation_certs } — resolution context for the subject's own edges
 * @param pinnedIssuer  Foundation pubkey (hex) to pin
 * @param opts.mode      REQUIRED: 'enforcing' | 'non-enforcing' (0006 enforce-or-label)
 * @param opts.heldEdges edge-records handed to the verifier (MUST honour, both modes)
 * @param opts.seek      REQUIRED iff enforcing: async (subjectFingerprintHex) => attestation[]
 *                       — Foundation-signed edge-records whose relates_to targets the subject
 *                       (a query against the caller's store; @geiant/core + read surface implement this)
 * Returns { valid, reason?, lineage_status?, superseded?, ...fields }.
 * @throws TypeError if mode is missing/invalid, or enforcing without a seek function.
 */
export async function verifyCGRAttestationV4(subject, ledger, pinnedIssuer, opts = {}) {
  const { mode, heldEdges = [], seek } = opts;
  if (mode !== 'enforcing' && mode !== 'non-enforcing')
    throw new TypeError('verifyCGRAttestationV4: opts.mode must be "enforcing" or "non-enforcing" (no silent default)');
  if (mode === 'enforcing' && typeof seek !== 'function')
    throw new TypeError('verifyCGRAttestationV4: enforcing mode requires opts.seek (a query function)');
  const fail = (reason) => ({ valid: false, reason });
  if (!subject || typeof subject !== 'object') return fail('no attestation');
  if (!pinnedIssuer) return fail('no pinned issuer key (fail closed)');
  if (subject.schema !== V4_SCHEMA) return fail(`unsupported schema: ${subject.schema}`);
  if (subject.issuer !== CGR_ISSUER) return fail(`issuer mismatch: ${subject.issuer}`);
  if (subject.issuer_key_id !== pinnedIssuer) return fail('issuer_key_id does not equal the pinned key');
  if (typeof subject.subject_key === 'string' && subject.subject_key === subject.issuer_key_id)
    return fail('subject_key equals issuer_key_id (neutrality violation)');

  // signature over the JCS-canonical signed body (relates_to IS in the body → catches B1, T9)
  if (!(await _sigOk(subject, pinnedIssuer))) return fail('signature verification failed');

  // grounding gate (§2.2): oracle_id/audit_policy required iff dimension ∈ GROUNDING_DIMENSIONS
  const isGrounding = GROUNDING_DIMENSIONS.has(subject.dimension);
  if (isGrounding) {
    if (subject.oracle_id === undefined) return fail('grounding attestation missing oracle_id');
    if (subject.audit_policy === undefined) return fail('grounding attestation missing audit_policy');
  } else {
    if (subject.oracle_id !== undefined) return fail('non-grounding attestation must not carry oracle_id');
    if (subject.audit_policy !== undefined) return fail('non-grounding attestation must not carry audit_policy');
    if (subject.n_unresolvable !== undefined) return fail('non-grounding attestation must not carry n_unresolvable');
  }

  // §2.2 0002 fields — UNIVERSAL presence gate. recorded_at (wire issue time) and verifiability_tag
  // are well-defined on every v4 record, so their presence is required.
  for (const f of ['verifiability_tag', 'recorded_at']) {
    if (subject[f] === undefined) return fail(`missing required field: ${f}`);
  }
  // verifiability_tag's VALUE is decidable (the absence gate references it) → gate it to the enum.
  if (subject.verifiability_tag !== 'judgment' && subject.verifiability_tag !== 'rule')
    return fail(`invalid verifiability_tag: ${subject.verifiability_tag}`);
  // §2.2 pooled-aggregate absence gate (enforced half) — domain / decision_date / backfilled are
  // PER-RECORD (a single area / a single dated decision). A pooled score has none → they MUST be
  // absent (reject if present), mirroring the grounding gate. Their SEMANTICS/ordering stay
  // stated-not-enforced (§2.4). Unenforced half — REQUIRED on `rule` records — has no gate yet
  // (nothing mints a rule record).
  if (subject.scoring_scope === 'pooled') {
    for (const f of ['domain', 'decision_date', 'backfilled']) {
      if (subject[f] !== undefined)
        return fail(`${f} must be absent on a pooled judgment aggregate (scoring_scope="pooled")`);
    }
  }

  // relates_to: per-edge validation (§1.1) — type, kind, per-kind hash_alg, hash format
  const edges = Array.isArray(subject.relates_to) ? subject.relates_to : [];
  for (const e of edges) {
    if (!e || !REL_TYPES.has(e.type)) return fail(`unrecognized relation type: ${e && e.type}`);
    const t = e.target;
    if (!t || !Object.prototype.hasOwnProperty.call(HASH_ALG_FOR_KIND, t.kind))
      return fail(`invalid target kind: ${t && t.kind}`);
    if (t.hash_alg !== HASH_ALG_FOR_KIND[t.kind])
      return fail(`hash_alg ${t.hash_alg} invalid for kind ${t.kind}`);
    if (typeof t.hash !== 'string' || !HEX64.test(t.hash))
      return fail(`malformed target hash for ${t.hash_alg}`);
    // §1.1 evidence_tier: REQUIRED on continues (closed vocab); MUST be absent on supersedes/revokes.
    if (e.type === 'continues') {
      if (e.evidence_tier === undefined) return fail('continues edge missing evidence_tier');
      if (!EVIDENCE_TIERS.has(e.evidence_tier)) return fail(`invalid evidence_tier: ${e.evidence_tier}`);
    } else if (e.evidence_tier !== undefined) {
      return fail('evidence_tier is only valid on continues edges');
    }
  }
  // multiplicity (§1.1): exact duplicate → reject; >1 continues → reject
  const seen = new Set();
  for (const e of edges) {
    const k = e.type + '|' + e.target.hash;
    if (seen.has(k)) return fail('duplicate {type,target} edge');
    seen.add(k);
  }
  if (edges.filter((e) => e.type === 'continues').length > 1)
    return fail('more than one continues edge (>1 lineage predecessor)');

  // traversal (§1.3)
  const tr = _traverse(subject, ledger);
  if (tr.reject) return fail(tr.reject);

  // held + sought edges (§1.3/§3): honour edge-records that target the subject.
  //   held = HANDED to the verifier — honoured in BOTH modes.
  //   seek = QUERIED for edges targeting the subject — ENFORCING mode only (0006).
  // Same processing once obtained; they differ only in provenance and when consulted.
  const subjFp = attestationFingerprint(subject);
  const toHonour = [...(heldEdges || [])];
  if (mode === 'enforcing') {
    let sought;
    try {
      sought = await seek(subjFp);
    } catch (e) {
      // Validity-Fails-Closed: an enforcing verifier that cannot complete the query cannot
      // determine revocation status → REJECT (never silently "valid"). Distinct from an ordinary
      // revocation — otherwise enforcement degrades to non-enforcement on every DB hiccup, the exact
      // failure 0006 exists to prevent.
      return fail(`revocation status undeterminable (seek failed: ${e && e.message ? e.message : e})`);
    }
    if (!Array.isArray(sought))
      return fail('revocation status undeterminable (seek returned a non-array)');
    toHonour.push(...sought);
  }
  let superseded = false;
  for (const rec of toHonour) {
    if (!rec || rec.issuer !== CGR_ISSUER) continue;
    if (!(await _sigOk(rec, pinnedIssuer))) continue;     // only a Foundation-signed edge-record binds
    for (const e of (Array.isArray(rec.relates_to) ? rec.relates_to : [])) {
      if (e.target && e.target.kind === 'attestation' && e.target.hash === subjFp) {
        if (e.type === 'revokes') return fail('subject is revoked (an edge targeting it was honoured)');
        if (e.type === 'supersedes') superseded = true;
      }
    }
  }

  const out = {
    valid: true,
    subjectKey: subject.subject_key,
    dimension: subject.dimension,
    score: subject.cgr_score,
    schema: subject.schema,
  };
  if (tr.lineage_status) out.lineage_status = tr.lineage_status;
  if (superseded) out.superseded = true;
  // §1.1 surface the continues edge's evidence_tier (recorded, non-gating; at most one continues).
  const continuesEdge = edges.find((e) => e.type === 'continues');
  if (continuesEdge) out.evidence_tier = continuesEdge.evidence_tier;
  return out;
}
