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
