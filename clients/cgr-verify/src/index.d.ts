export declare const CGR_ISSUER: string;
export declare const ACCEPTED_SCHEMAS: Set<string>;

/** Canonical (RFC 8785 / JCS) bytes of the signed body (excludes signature + evidence_ref). */
export declare function canonCGRBody(att: Record<string, unknown>): Uint8Array;

export interface VerifyOptions {
  /** If set, att.subject_key must equal this (identity binding). */
  expectedKey?: string;
  /** Optional freshness gate (ms) on last_resolved_at. */
  maxAgeMs?: number;
  /** Override "now" for the freshness gate (ms since epoch). */
  nowMs?: number;
}

export interface VerifyResult {
  valid: boolean;
  reason?: string;
  subjectKey?: string;
  subjectDid?: string;
  dimension?: string;
  score?: number;
  evidenceMass?: number;
  nResolved?: number;
  scoringScope?: string;
  requestedDomain?: string | null;
  domainNResolved?: number | null;
  lastResolvedAt?: string | null;
  schema?: string;
}

/** Verify a CGR attestation offline against a pinned Foundation public key (hex). */
export declare function verifyCGRAttestation(
  att: Record<string, unknown>,
  pinnedIssuerPubKeyHex: string,
  opts?: VerifyOptions,
): Promise<VerifyResult>;

// ── cgr.attestation.v4 ──────────────────────────────────────────────────────
export declare const V4_SCHEMA: string;
export declare const GROUNDING_DIMENSIONS: Set<string>;

export type LineageStatus =
  | 'complete' | 'truncated_unavailable' | 'truncated_depth' | 'anomaly_cycle';

/** §1.1 authority tier that substantiated a `continues` edge — closed vocabulary, descending strength. */
export type EvidenceTier = 'custody_record' | 'issuer_records' | 'operator_verification';

export interface V4Ledger {
  attestations?: Record<string, Record<string, unknown>>;
  delegation_certs?: Record<string, Record<string, unknown>>;
}

export interface VerifyResultV4 extends VerifyResult {
  /** §1.3 lineage signal (present when the subject carries a `continues` edge).
   *  snake_case to match the conformance corpus `expect` keys. */
  lineage_status?: LineageStatus;
  /** True when a `supersedes` edge (held, or sought in enforcing mode) targets the subject:
   *  signature-valid but not current. Present only when true. Distinct from `valid: false`. */
  superseded?: boolean;
  /** §1.1 the authority tier the Foundation attests substantiated the subject's `continues` edge —
   *  SURFACED, not gated (sufficiency is the relying party's call). Present when a `continues` edge
   *  exists. A recorded CLAIM about the issuer's own evidence, not independent proof. */
  evidence_tier?: EvidenceTier;
}

/** BLAKE2b-256 fingerprint of an attestation's canonical signed body (§1.1). */
export declare function attestationFingerprint(att: Record<string, unknown>): string;

export type EnforcementMode = 'enforcing' | 'non-enforcing';

export interface VerifyV4Options {
  /** REQUIRED — explicit at the call site, no silent default (decision 0006 enforce-or-label). */
  mode: EnforcementMode;
  /** Edge-records HANDED to the verifier; honoured in BOTH modes. */
  heldEdges?: Array<Record<string, unknown>>;
  /** REQUIRED iff mode === 'enforcing'. Query the caller's store for Foundation-signed
   *  edge-records whose relates_to targets the subject. If it throws, the verifier rejects
   *  with "revocation status undeterminable" (Validity-Fails-Closed). */
  seek?: (subjectFingerprintHex: string) => Promise<Array<Record<string, unknown>>>;
}

/**
 * Verify a cgr.attestation.v4 attestation offline. Async in BOTH modes.
 * @throws TypeError if `mode` is missing/invalid, or enforcing without `seek`.
 */
export declare function verifyCGRAttestationV4(
  subject: Record<string, unknown>,
  ledger: V4Ledger,
  pinnedIssuerPubKeyHex: string,
  opts: VerifyV4Options,
): Promise<VerifyResultV4>;

// ── cgr.cosign.v1 — two-party co-signature envelope ─────────────────────────
export declare const COSIGN_SCHEMA: string;
/** §9 domain tag the approver signature is bound under (`grafomem.hitl.approval.v1:`). */
export declare const DOMAIN_TAG: string;

/** §2.1 content digest of a content_body — `b2-256:` + hex(BLAKE2b-256(JCS(body))). */
export declare function contentDigest(body: unknown): string;

/** §5.1 required-ness predicate: a single (field-path, op, literal) triple over content_body. */
export interface CosignPredicate {
  field: string;
  op: 'eq' | 'ne' | 'in' | 'lt' | 'lte' | 'gt' | 'gte';
  /** Scalar for eq/ne/ordering; an ARRAY for `in` (a non-array `in` value is UNDETERMINED → reject). */
  value: unknown;
}

/** A profile entry in the registry. `approver_signature` is either unconditional `'REQUIRED'`
 *  or a predicate that makes it required only when it holds (§5, §5.1). */
export interface CosignProfile {
  approval_mode: 'bound' | 'free';
  approver_signature: 'REQUIRED' | { required_when: CosignPredicate };
  referenced_records?: 'none' | 'required' | string;
}

/** The profile registry — profile name → profile. §8 step 2 resolves against it (fail closed on
 *  unknown). The registry DOCUMENT is unwritten (spec §11 Q3); see the README for the shape and a
 *  minimal working example. */
export interface CosignRegistry {
  profiles: Record<string, CosignProfile>;
}

export interface CosignLedger {
  /** Seen (approver_key_id, record_nonce) pairs — a duplicate is a replay (§4/§8.7). */
  seen?: Array<[string, string]>;
  /** Free-mode: reference hashes the consumer can resolve; unresolved ones degrade (§8.9). */
  resolvable?: string[];
}

/** The REQUIRED trusted issuer set (§8.2a, decision 0011): issuer key ids (`ed25519:<hex>` or bare
 *  hex). NO default and NO trust-everything path — absent or empty ⇒ the verifier rejects, never
 *  falling back to the record's self-declared issuer. */
export type TrustedIssuers = Iterable<string>;

export interface VerifyCosignResult {
  valid: boolean;
  /** Present on rejection. `issuer_untrusted` (§8.2a), `predicate_unresolved` (§5.1/§8.3a),
   *  `content_digest mismatch`, `system signature verification failed`, etc. */
  reason?: string;
  /** MUST-surface / MUST-NOT-gate fields (§8): approver identity, act, date, and the assurance
   *  tier (how strongly the approver key is bound to a named person — established elsewhere, gap 3a).
   *  A `valid` verdict does NOT mean high assurance; tier policy is the relying party's to enforce. */
  surfaced?: {
    approver_id?: string;
    approver_act?: 'approve' | 'modify' | 'override';
    decision_date?: string;
    assurance_tier: null;
  };
  /** Free mode only: a well-formed reference could not be resolved (§8.9 degrade — not a rejection). */
  references_unresolved?: boolean;
}

/**
 * Verify a cgr.cosign.v1 two-party co-signature record offline. §8 ordered checks, failing closed.
 * `trustedIssuers` is REQUIRED (§8.2a) — omitting it or passing an empty set rejects; the issuer is
 * checked against it BEFORE the system signature, never trusted on the record's own say-so.
 */
export declare function verifyCosign(
  record: Record<string, unknown>,
  registry: CosignRegistry,
  ledger: CosignLedger | undefined,
  trustedIssuers: TrustedIssuers,
): Promise<VerifyCosignResult>;
