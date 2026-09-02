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
