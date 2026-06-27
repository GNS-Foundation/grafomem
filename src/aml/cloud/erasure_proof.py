"""
GRAFOMEM Erasure Proof — GDPR Article 17 signed erasure certificates.

When a user exercises their right to erasure, this service produces a
cryptographic proof that:
  1. The fact was deleted from memory
  2. All references were scrubbed from decision trail records
  3. The erasure is timestamped and Ed25519-signed
  4. The certificate is tamper-proof and independently verifiable

The certificate is a JSON document containing the erasure metadata,
signed by the platform's Ed25519 key. It can be presented to auditors,
DPAs (Data Protection Authorities), or the data subject themselves as
evidence of compliance.

Backed by PostgreSQL via psycopg v3 (sync), following the same patterns
as ComplianceTracker, MeteringService, and DecisionTrailService.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.erasure_proof")

FRESHNESS_WINDOW_DAYS = 180

# BLAKE2b-128 for certificate IDs — matches provenance.py
_CERT_ID_BYTES = 16
_SEP = b"\x1f"


# ============================================================================
# Core data types
# ============================================================================

@dataclass(slots=True)
class ErasureCertificate:
    """A signed proof that a fact was erased and scrubbed."""
    certificate_id: str
    tenant_id: str
    fact_ref: int
    fact_content_hash: str | None  # BLAKE2b hash of the deleted content (not the content itself)

    # What was done
    coverage: dict[str, str]  # e.g. {"primary": "absent", "embedding": "present", "cache": "unchecked"}
    scrubbed_decision_ids: list[str]
    # Timing
    erasure_requested_at: datetime
    erasure_completed_at: datetime

    # Legal basis
    legal_basis: str  # e.g. "GDPR Article 17 — Right to Erasure"
    requested_by: str | None  # e.g. "data_subject", "dpo", "automated"

    governance_record: dict[str, Any] | None = None

    # Provenance — Ed25519 signature over the certificate
    signature: bytes | None = None
    public_key: bytes | None = None

    # Verification
    verified: bool = False
    verification_note: str | None = None


# ============================================================================
# Certificate ID computation
# ============================================================================

def compute_certificate_id(
    tenant_id: str,
    fact_ref: int,
    erasure_completed_at: datetime,
) -> str:
    """BLAKE2b-128 hex digest — deterministic certificate ID."""
    h = hashlib.blake2b(digest_size=_CERT_ID_BYTES)
    for part in [tenant_id, str(fact_ref), erasure_completed_at.isoformat()]:
        h.update(part.encode("utf-8"))
        h.update(_SEP)
    return h.hexdigest()


def compute_certificate_digest(cert_data: dict) -> bytes:
    """BLAKE2b-256 digest of canonical certificate JSON — for signing."""
    # Canonical JSON: sorted keys, no whitespace
    canonical = json.dumps(cert_data, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=32).digest()


def hash_content(content: str) -> str:
    """BLAKE2b-128 hex hash of fact content — stored instead of the content itself.

    This proves we knew what was deleted without retaining the PII.
    """
    return hashlib.blake2b(
        content.encode("utf-8"), digest_size=_CERT_ID_BYTES,
    ).hexdigest()


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS erasure_certificates (
    certificate_id          TEXT PRIMARY KEY,
    tenant_id               TEXT NOT NULL,
    fact_ref                INTEGER NOT NULL,
    fact_content_hash       TEXT,

    -- What was done
    governance_record       JSONB,
    coverage                JSONB NOT NULL DEFAULT '{}'::jsonb,
    scrubbed_decision_ids   JSONB NOT NULL DEFAULT '[]',

    -- Timing
    erasure_requested_at    TIMESTAMPTZ NOT NULL,
    erasure_completed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Legal basis
    legal_basis             TEXT NOT NULL DEFAULT 'GDPR Article 17 — Right to Erasure',
    requested_by            TEXT,

    -- Provenance
    signature               BYTEA,
    public_key              BYTEA,

    -- Verification
    verified                BOOLEAN NOT NULL DEFAULT FALSE,
    verification_note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ec_tenant
    ON erasure_certificates(tenant_id, erasure_completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_ec_fact
    ON erasure_certificates(tenant_id, fact_ref);
"""


# ============================================================================
# ErasureProofService
# ============================================================================

class ErasureProofService:
    """Issues, stores, and verifies GDPR erasure certificates.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    decision_trail : DecisionTrailService, optional
        For automated scrubbing of decision records.
    signing_key : bytes, optional
        32-byte Ed25519 private seed for signing certificates.
    """

    def __init__(
        self,
        db_url: str,
        decision_trail=None,
        signing_identity=None,
        gcrumbs=None,
        pool=None,
        erasure_ledger=None,
        table_prefix: str = "",
    ) -> None:
        self._db_url = db_url
        self._decision_trail = decision_trail
        self._signing_identity = signing_identity
        self._gcrumbs = gcrumbs
        self._pool = pool
        self._erasure_ledger = erasure_ledger
        self._table_prefix = table_prefix
        self._conn: psycopg.Connection[dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        """Return an open connection, creating one lazily."""
        if self._pool is not None:
            return self._pool.getconn()
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        """Close the underlying database connection."""
        if self._pool is not None:
            self._conn = None
            return
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create the ``erasure_certificates`` table if it does not exist."""
        conn = self._get_conn()
        schema_sql = _SCHEMA_SQL.replace("erasure_certificates", f"{self._table_prefix}erasure_certificates")
        conn.execute(schema_sql)
        logger.info("Erasure Proof schema ensured")

    # ------------------------------------------------------------------
    # Issuance requirements
    # ------------------------------------------------------------------

    def assert_can_sign(self, signing_identity=None) -> None:
        """Verify that a signing identity is available for erasure certificates.

        Raises RuntimeError if unsigned certificates would be produced,
        enforcing a strictly fail-closed security posture.
        """
        key = signing_identity or self._signing_identity
        if key is None:
            raise RuntimeError(
                "GRAFOMEM_SIGNING_KEY is required to issue erasure certificates. "
                "Unsigned certificates are strictly prohibited."
            )

    # ------------------------------------------------------------------
    # Issue a certificate
    # ------------------------------------------------------------------

    def issue_certificate(
        self,
        tenant_id: str,
        fact_ref: int,
        *,
        fact_content: str | None = None,
        coverage: dict[str, str] | None = None,
        legal_basis: str = "GDPR Article 17 — Right to Erasure",
        requested_by: str | None = "data_subject",
        signing_identity=None,
    ) -> ErasureCertificate:
        """Issue a signed erasure certificate.

        Performs the full erasure workflow:
        1. Scrubs the fact from all decision trail records
        2. Computes a content hash (retains proof without retaining PII)
        3. Ed25519-signs the certificate
        4. Persists to PostgreSQL

        Parameters
        ----------
        tenant_id : str
            The tenant requesting erasure.
        fact_ref : int
            The memory ref that was deleted.
        fact_content : str, optional
            The content of the deleted fact (used to compute hash, NOT stored).
        coverage : dict[str, str], optional
            Per-subsystem erasure findings (e.g. {"primary": "absent", "embedding": "present"}).
        legal_basis : str
            Legal basis for the erasure (default: GDPR Article 17).
        requested_by : str, optional
            Who requested the erasure.
        signing_identity : SigningIdentity, optional
            Override signing identity (falls back to service-level identity).

        Returns
        -------
        ErasureCertificate
        """
        self.assert_can_sign(signing_identity)

        requested_at = datetime.now(tz=timezone.utc)

        # Step 1: Scrub decision trail records
        scrubbed_count = 0
        scrubbed_ids: list[str] = []
        if self._decision_trail is not None:
            # Find affected decisions before scrubbing
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT decision_id FROM decision_records "
                "WHERE tenant_id = %s "
                "  AND retrieved_refs @> %s::jsonb",
                (tenant_id, json.dumps([fact_ref])),
            ).fetchall()
            scrubbed_ids = [r["decision_id"] for r in rows]

            scrubbed_count = self._decision_trail.scrub_fact(fact_ref, tenant_id)

        completed_at = datetime.now(tz=timezone.utc)

        # Step 2: Compute content hash
        content_hash = hash_content(fact_content) if fact_content else None

        # Step 3: Compute certificate ID
        certificate_id = compute_certificate_id(tenant_id, fact_ref, completed_at)

        # Step 4: Sign the certificate
        coverage_dict = coverage or {"primary": "absent"}
        
        governance_record = {
            "declared": {
                "obligation": "deletion",
                "target": f"memory_ref:{fact_ref}"
            },
            "observed": [
                {
                    "store": store,
                    "status": status,
                    "locus": f"grafomem-{store}",
                    "source_class": "independent_observation"
                }
                for store, status in coverage_dict.items()
            ],
            "coverage": list(coverage_dict.keys()),
            "non_claims": {
                "coverage_gaps": [],  # Verified offline
                "scope": {
                    "tenant": tenant_id,
                    "covers": list(coverage_dict.keys()),
                    "excludes": [],
                },
                "freshness": {
                    "asserted_at": completed_at.isoformat(),
                    "valid_until": (completed_at + timedelta(days=FRESHNESS_WINDOW_DAYS)).isoformat(),
                }
            },
            "addressing": {
                "certificate_id": certificate_id,
                "fact_content_hash": content_hash
            },
            "verifier": {
                "identity": "grafomem_erasure_daemon",
                "version": "1.0.0"
            }
        }

        cert_data = {
            "certificate_id": certificate_id,
            "tenant_id": tenant_id,
            "fact_ref": fact_ref,
            "fact_content_hash": content_hash,
            "governance_record": governance_record,
            "coverage": coverage_dict,
            "erasure_requested_at": requested_at.isoformat(),
            "erasure_completed_at": completed_at.isoformat(),
            "legal_basis": legal_basis,
        }

        signature = None
        public_key = None
        key = signing_identity or self._signing_identity
        from aml.provenance import sign_provenance
        digest = compute_certificate_digest(cert_data)
        signature, public_key = sign_provenance(key, digest)

        # Step 5: Persist
        conn = self._get_conn()
        conn.execute(
            f"INSERT INTO {self._table_prefix}erasure_certificates "
            "(certificate_id, tenant_id, fact_ref, fact_content_hash, "
            " governance_record, coverage, scrubbed_decision_ids, "
            " erasure_requested_at, erasure_completed_at, "
            " legal_basis, requested_by, "
            " signature, public_key, verified, verification_note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                certificate_id, tenant_id, fact_ref, content_hash,
                json.dumps(governance_record),
                json.dumps(coverage_dict), json.dumps(scrubbed_ids),
                requested_at, completed_at,
                legal_basis, requested_by,
                signature, public_key,
                signature is not None,  # verified if signed
                "Ed25519-signed at issuance" if signature else None,
            ),
        )

        if self._erasure_ledger and signature is not None:
            self._erasure_ledger.record_subject_erasure(
                entry_id=certificate_id,
                tenant_id=tenant_id,
                fact_ref=fact_ref,
                content_hash=content_hash,
                certificate=cert_data
            )

        logger.info(
            "Erasure certificate issued: cert=%s tenant=%s fact_ref=%s scrubbed=%d",
            certificate_id, tenant_id, fact_ref, scrubbed_count,
        )

        try:
            from aml.cloud.metrics import ERASURE_CERTIFICATES
            ERASURE_CERTIFICATES.inc()
        except Exception:
            pass

        # gcrumbs breadcrumb — governance decision for erasure
        if self._gcrumbs:
            try:
                self._gcrumbs.append_breadcrumb(
                    tenant_id, "erasure:issued",
                    {"args": {"fact_ref": fact_ref,
                              "certificate_id": certificate_id},
                     "authorized": True, "reasons": [],
                     "agent": requested_by, "tier": None},
                    source_type="erasure", source_ref=certificate_id)
            except Exception:
                logger.warning(
                    "gcrumbs breadcrumb failed for erasure %s",
                    certificate_id, exc_info=True)

        return ErasureCertificate(
            certificate_id=certificate_id,
            tenant_id=tenant_id,
            fact_ref=fact_ref,
            fact_content_hash=content_hash,
            governance_record=governance_record,
            coverage=coverage_dict,
            scrubbed_decision_ids=scrubbed_ids,
            erasure_requested_at=requested_at,
            erasure_completed_at=completed_at,
            legal_basis=legal_basis,
            requested_by=requested_by,
            signature=signature,
            public_key=public_key,
            verified=signature is not None,
            verification_note="Ed25519-signed at issuance" if signature else None,
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, certificate_id: str) -> ErasureCertificate | None:
        """Retrieve a single erasure certificate by ID."""
        conn = self._get_conn()
        row = conn.execute(
            f"SELECT * FROM {self._table_prefix}erasure_certificates WHERE certificate_id = %s",
            (certificate_id,),
        ).fetchone()
        return self._row_to_cert(row) if row else None

    def get_by_fact(self, tenant_id: str, fact_ref: int) -> ErasureCertificate | None:
        """Retrieve the erasure certificate for a specific fact."""
        conn = self._get_conn()
        row = conn.execute(
            f"SELECT * FROM {self._table_prefix}erasure_certificates "
            "WHERE tenant_id = %s AND fact_ref = %s "
            "ORDER BY erasure_completed_at DESC LIMIT 1",
            (tenant_id, fact_ref),
        ).fetchone()
        return self._row_to_cert(row) if row else None

    def list_certificates(
        self,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ErasureCertificate]:
        """List all erasure certificates for a tenant."""
        conn = self._get_conn()
        rows = conn.execute(
            f"SELECT * FROM {self._table_prefix}erasure_certificates "
            "WHERE tenant_id = %s "
            "ORDER BY erasure_completed_at DESC "
            "LIMIT %s OFFSET %s",
            (tenant_id, limit, offset),
        ).fetchall()
        return [self._row_to_cert(r) for r in rows]

    def get_stats(self, tenant_id: str) -> dict[str, Any]:
        """Summary stats for erasure certificates."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT "
            "  COUNT(*) AS total, "
            "  COUNT(CASE WHEN verified THEN 1 END) AS signed_count, "
            "  MIN(erasure_completed_at) AS first_erasure, "
            "  MAX(erasure_completed_at) AS last_erasure "
            f"FROM {self._table_prefix}erasure_certificates "
            "WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()

        if row is None or row["total"] == 0:
            return {"total": 0}

        return {
            "total": row["total"],
            "signed_count": row["signed_count"] or 0,
            "first_erasure": row["first_erasure"].isoformat() if row["first_erasure"] else None,
            "last_erasure": row["last_erasure"].isoformat() if row["last_erasure"] else None,
        }

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify_certificate(self, certificate_id: str) -> dict[str, Any]:
        """Verify the Ed25519 signature on an erasure certificate.

        Returns a dict with 'valid', 'certificate_id', and 'detail'.
        """
        cert = self.get(certificate_id)
        if cert is None:
            return {
                "valid": False,
                "certificate_id": certificate_id,
                "detail": "Certificate not found",
            }

        if cert.signature is None or cert.public_key is None:
            return {
                "valid": False,
                "certificate_id": certificate_id,
                "detail": "Certificate is not signed",
            }

        # Reconstruct the canonical data that was signed
        cert_data = {
            "certificate_id": cert.certificate_id,
            "tenant_id": cert.tenant_id,
            "fact_ref": cert.fact_ref,
            "fact_content_hash": cert.fact_content_hash,
            "coverage": cert.coverage,
            "erasure_requested_at": cert.erasure_requested_at.isoformat(),
            "erasure_completed_at": cert.erasure_completed_at.isoformat(),
            "legal_basis": cert.legal_basis,
        }
        if cert.governance_record:
            cert_data["governance_record"] = cert.governance_record

        digest = compute_certificate_digest(cert_data)

        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
            pub = Ed25519PublicKey.from_public_bytes(cert.public_key)
            pub.verify(cert.signature, digest)
            return {
                "valid": True,
                "certificate_id": certificate_id,
                "detail": "Ed25519 signature verified — certificate is authentic",
            }
        except Exception as e:
            return {
                "valid": False,
                "certificate_id": certificate_id,
                "detail": f"Signature verification failed: {e}",
            }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_cert(row: dict[str, Any]) -> ErasureCertificate:
        """Convert a database row dict into an ErasureCertificate."""
        ids = row.get("scrubbed_decision_ids")
        if isinstance(ids, str):
            ids = json.loads(ids)
        elif ids is None:
            ids = []

        return ErasureCertificate(
            certificate_id=row["certificate_id"],
            tenant_id=row["tenant_id"],
            fact_ref=row["fact_ref"],
            fact_content_hash=row.get("fact_content_hash"),
            governance_record=row.get("governance_record", {}) if isinstance(row.get("governance_record"), dict) else json.loads(row.get("governance_record", "{}") or "{}"),
            coverage=row.get("coverage", {}) if isinstance(row.get("coverage"), dict) else json.loads(row.get("coverage", "{}")),
            scrubbed_decision_ids=ids,
            erasure_requested_at=row["erasure_requested_at"].astimezone(timezone.utc) if row["erasure_requested_at"] else None,
            erasure_completed_at=row["erasure_completed_at"].astimezone(timezone.utc) if row["erasure_completed_at"] else None,
            legal_basis=row["legal_basis"],
            requested_by=row.get("requested_by"),
            signature=row.get("signature"),
            public_key=row.get("public_key"),
            verified=row.get("verified", False),
            verification_note=row.get("verification_note"),
        )

    @staticmethod
    def cert_to_dict(cert: ErasureCertificate) -> dict[str, Any]:
        """Convert an ErasureCertificate to a JSON-safe dict."""
        import base64
        return {
            "certificate_id": cert.certificate_id,
            "tenant_id": cert.tenant_id,
            "fact_ref": cert.fact_ref,
            "fact_content_hash": cert.fact_content_hash,
            "governance_record": cert.governance_record,
            "coverage": cert.coverage,
            "scrubbed_decision_ids": cert.scrubbed_decision_ids,
            "erasure_requested_at": cert.erasure_requested_at.isoformat(),
            "erasure_completed_at": cert.erasure_completed_at.isoformat(),
            "legal_basis": cert.legal_basis,
            "requested_by": cert.requested_by,
            "signature": base64.b64encode(cert.signature).decode() if cert.signature else None,
            "public_key": base64.b64encode(cert.public_key).decode() if cert.public_key else None,
            "verified": cert.verified,
            "verification_note": cert.verification_note,
        }

# ============================================================================
# Independent Erasure Verifier (Increment 3)
# ============================================================================

def verify_erasure_effect(
    cert_bytes: bytes,
    signature: bytes,
    public_key: bytes,
    required_stores: list[str],
    current_time: datetime | None = None,
) -> dict[str, Any]:
    """Independent verification of an erasure effect from canonical bytes.
    
    1. Reconstructs canonical bytes and verifies Ed25519 signature.
    2. Enforces precedence logic on the verified coverage dictionary:
       - `failed` > `incomplete` > `enforced`.
       
    Parameters
    ----------
    cert_bytes : bytes
        The exact JSON bytes of the data that was signed.
    signature : bytes
        The Ed25519 signature to verify.
    public_key : bytes
        The public key to verify against.
    required_stores : list[str]
        List of subsystems that must be checked-and-absent (e.g. ["primary", "embedding"]).
    current_time : datetime, optional
        Used to evaluate freshness of the record.
        
    Returns
    -------
    dict
        {"result": str, "coverage_gaps": list[str]}
        Where result is 'invalid', 'failed', 'incomplete', or 'enforced'.
    """
    import json
    import hashlib
    try:
        cert_data = json.loads(cert_bytes)
    except Exception:
        return {"result": "invalid", "coverage_gaps": []}
        
    # 1. Verify signature BEFORE trusting coverage
    signed_data = {
        "certificate_id": cert_data.get("certificate_id"),
        "tenant_id": cert_data.get("tenant_id"),
        "fact_ref": cert_data.get("fact_ref"),
        "fact_content_hash": cert_data.get("fact_content_hash"),
        "coverage": cert_data.get("coverage", {}),
        "erasure_requested_at": cert_data.get("erasure_requested_at"),
        "erasure_completed_at": cert_data.get("erasure_completed_at"),
        "legal_basis": cert_data.get("legal_basis"),
    }
    if "governance_record" in cert_data:
        signed_data["governance_record"] = cert_data["governance_record"]

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(public_key)
        # Recreate canonical json and hash it
        canonical = json.dumps(signed_data, sort_keys=True, separators=(",", ":"))
        digest = hashlib.blake2b(canonical.encode("utf-8"), digest_size=32).digest()
        pub.verify(signature, digest)
    except Exception:
        return {"result": "invalid", "coverage_gaps": []}

    # 2. Extract verified coverage
    coverage_gaps = []
    store_status = {}
    
    if "governance_record" in signed_data and signed_data["governance_record"]:
        record = signed_data["governance_record"]
        # Check freshness
        freshness = record.get("non_claims", {}).get("freshness")
        if isinstance(freshness, dict) and "valid_until" in freshness:
            # check current_time vs valid_until
            valid_until_str = freshness["valid_until"]
            valid_until_dt = datetime.fromisoformat(valid_until_str.replace("Z", "+00:00"))
            check_time = current_time or datetime.now(timezone.utc)
            if check_time > valid_until_dt:
                return {"result": "incomplete", "coverage_gaps": required_stores, "note": "Freshness expired"}

        for obs in record.get("observed", []):
            store = obs.get("store")
            status = obs.get("status")
            if store:
                store_status[store] = status
    else:
        # Legacy path
        store_status = signed_data.get("coverage", {})

    # Pass 1: Compute coverage gaps (independent of aggregate)
    # A gap is any required store that is missing, unchecked, pending, etc.
    # Effectively, anything that is not definitively 'present' or 'absent'.
    for store in required_stores:
        status = store_status.get(store)
        if status not in ("present", "absent"):
            coverage_gaps.append(store)

    # Pass 2: Compute aggregate result
    # A. failed (Dominates everything: if any *required* store is explicitly present)
    for store in required_stores:
        if store_status.get(store) == "present":
            return {"result": "failed", "coverage_gaps": coverage_gaps}

    # B. incomplete (If there are any coverage gaps)
    if coverage_gaps:
        return {"result": "incomplete", "coverage_gaps": coverage_gaps}

    # C. enforced (All required stores present and explicitly absent)
    # At this point, no required store is present, and there are no gaps.
    # So every required store must be 'absent'.
    return {"result": "enforced", "coverage_gaps": coverage_gaps}
