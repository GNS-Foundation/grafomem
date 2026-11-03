"""
GRAFOMEM Regulatory Reports — one-click compliance audit packages.

Generates structured reports for regulatory frameworks:
  - EU AI Act (Article 13: Transparency, Article 14: Human Oversight)
  - GDPR (Article 17: Erasure, Article 30: Records of Processing)
  - DORA (Digital Operational Resilience Act — ICT risk)

Each report aggregates data from Decision Trail, Erasure Proof,
Governance Gateway, and Compliance services into a downloadable
audit package.

Backed by PostgreSQL via psycopg v3 (sync).
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger("grafomem.cloud.regulatory")


# ============================================================================
# Enumerations
# ============================================================================

class ReportType(str, Enum):
    EU_AI_ACT = "eu_ai_act"
    GDPR = "gdpr"
    DORA = "dora"
    FULL_AUDIT = "full_audit"  # All frameworks combined


class ReportStatus(str, Enum):
    GENERATING = "generating"
    COMPLETE = "complete"
    FAILED = "failed"


# ============================================================================
# Core data types
# ============================================================================

@dataclass(slots=True)
class Report:
    """A generated regulatory report."""
    report_id: str
    tenant_id: str
    report_type: ReportType
    title: str
    status: ReportStatus
    period_start: datetime
    period_end: datetime
    content: dict[str, Any]  # The structured report data
    content_hash: str | None = None  # BLAKE2b-256 for tamper detection
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    file_size_bytes: int = 0


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS regulatory_reports (
    report_id       TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    report_type     TEXT NOT NULL,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'generating',
    period_start    TIMESTAMPTZ NOT NULL,
    period_end      TIMESTAMPTZ NOT NULL,
    content         JSONB NOT NULL DEFAULT '{}',
    content_hash    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    file_size_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_rr_tenant
    ON regulatory_reports(tenant_id, created_at DESC);
"""


# ============================================================================
# RegulatoryReportService
# ============================================================================

class RegulatoryReportService:
    """Generates one-click regulatory compliance reports.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    decision_trail : DecisionTrailService, optional
    erasure_proof : ErasureProofService, optional
    governance : GovernanceGateway, optional
    compliance : ComplianceTracker, optional
    """

    def __init__(
        self,
        db_url: str,
        decision_trail=None,
        erasure_proof=None,
        governance=None,
        compliance=None,
        pool=None,
    ) -> None:
        self._db_url = db_url
        self._dt = decision_trail
        self._ep = erasure_proof
        self._gov = governance
        self._comp = compliance
        self._pool = pool
        self._conn: psycopg.Connection[dict[str, Any]] | None = None

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        if self._pool is not None:
            return self._pool.getconn()
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._db_url, row_factory=dict_row, autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        if self._pool is not None:
            self._conn = None
            return
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    def ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.execute(_SCHEMA_SQL)
        logger.info("Regulatory Reports schema ensured")

    # ------------------------------------------------------------------
    # Generate reports
    # ------------------------------------------------------------------

    def generate(
        self,
        tenant_id: str,
        report_type: ReportType | str,
        period_days: int = 30,
    ) -> Report:
        """Generate a regulatory report.

        Parameters
        ----------
        tenant_id : str
        report_type : ReportType or str
        period_days : int
            Look-back period in days.
        """
        if isinstance(report_type, str):
            report_type = ReportType(report_type)

        now = datetime.now(tz=timezone.utc)
        period_start = now - timedelta(days=period_days)
        report_id = uuid.uuid4().hex[:24]

        generators = {
            ReportType.EU_AI_ACT: self._gen_eu_ai_act,
            ReportType.GDPR: self._gen_gdpr,
            ReportType.DORA: self._gen_dora,
            ReportType.FULL_AUDIT: self._gen_full_audit,
        }

        gen_fn = generators.get(report_type, self._gen_full_audit)

        try:
            content = gen_fn(tenant_id, period_start, now)
            content_json = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
            content_hash = hashlib.blake2b(
                content_json.encode(), digest_size=32,
            ).hexdigest()
            file_size = len(content_json.encode())

            title = self._title_for(report_type, period_start, now)

            report = Report(
                report_id=report_id,
                tenant_id=tenant_id,
                report_type=report_type,
                title=title,
                status=ReportStatus.COMPLETE,
                period_start=period_start,
                period_end=now,
                content=content,
                content_hash=content_hash,
                created_at=now,
                file_size_bytes=file_size,
            )

            self._persist(report)
            logger.info("Report generated: %s (%s) for %s", title, report_type.value, tenant_id)
            return report

        except Exception as e:
            logger.error("Report generation failed: %s", e)
            report = Report(
                report_id=report_id,
                tenant_id=tenant_id,
                report_type=report_type,
                title=f"Failed: {report_type.value}",
                status=ReportStatus.FAILED,
                period_start=period_start,
                period_end=now,
                content={"error": str(e)},
                created_at=now,
            )
            self._persist(report)
            return report

    # ------------------------------------------------------------------
    # Report generators
    # ------------------------------------------------------------------

    def _gen_eu_ai_act(
        self, tenant_id: str, start: datetime, end: datetime,
    ) -> dict[str, Any]:
        """EU AI Act compliance report.

        Covers Article 13 (Transparency) and Article 14 (Human Oversight).
        """
        report: dict[str, Any] = {
            "framework": "EU AI Act",
            "regulation": "Regulation (EU) 2024/1689",
            "generated_at": end.isoformat(),
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "sections": {},
        }

        # Article 13 — Transparency (Decision Trail)
        dt_stats = self._safe_dt_stats(tenant_id)
        report["sections"]["article_13_transparency"] = {
            "title": "Article 13 — Transparency Obligations",
            "requirement": "AI systems shall be designed to enable users to interpret "
                          "the system's output and use it appropriately.",
            "compliance_evidence": {
                "decision_trail_active": dt_stats.get("total", 0) > 0,
                "total_decisions_logged": dt_stats.get("total", 0),
                "models_used": dt_stats.get("models_used"),
                "average_latency_ms": dt_stats.get("avg_latency_ms"),
                "replay_capability": True,
                "fact_provenance": "BLAKE2b-128 content-addressed, Ed25519-signed",
            },
            "finding": "COMPLIANT" if dt_stats.get("total", 0) > 0 else "INSUFFICIENT_DATA",
        }

        # Article 14 — Human Oversight (Governance Gateway)
        gov_stats = self._safe_gov_stats(tenant_id)
        hitl_policies = self._count_hitl_policies(tenant_id)
        report["sections"]["article_14_human_oversight"] = {
            "title": "Article 14 — Human Oversight",
            "requirement": "AI systems shall be designed to allow human oversight, "
                          "including the ability to intervene or override.",
            "compliance_evidence": {
                "governance_gateway_active": gov_stats.get("policies_active", 0) > 0,
                "total_policies": gov_stats.get("policies_total", 0),
                "active_policies": gov_stats.get("policies_active", 0),
                "hitl_policies": hitl_policies,
                "evaluations_total": gov_stats.get("evaluations_total", 0),
                "requests_denied": gov_stats.get("evaluations_denied", 0),
                "requests_escalated_hitl": gov_stats.get("evaluations_escalated", 0),
            },
            "finding": "COMPLIANT" if hitl_policies > 0 else "PARTIAL",
        }

        # Article 15 — Accuracy, Robustness, Cybersecurity
        comp_data = self._safe_comp_data(tenant_id)
        report["sections"]["article_15_accuracy"] = {
            "title": "Article 15 — Accuracy, Robustness and Cybersecurity",
            "requirement": "Appropriate levels of accuracy, robustness, and cybersecurity.",
            "compliance_evidence": {
                "gmp_conformance_rate": comp_data.get("conformance_rate"),
                "capabilities_declared": comp_data.get("capabilities", []),
                "cryptographic_provenance": "Ed25519 signatures on decisions and erasure certificates",
                "content_addressing": "BLAKE2b-128 / BLAKE2b-256",
            },
            "finding": "COMPLIANT" if comp_data.get("conformance_rate", 0) >= 0.95 else "PARTIAL",
        }

        report["overall_finding"] = self._overall(report["sections"])
        return report

    def _gen_gdpr(
        self, tenant_id: str, start: datetime, end: datetime,
    ) -> dict[str, Any]:
        """GDPR compliance report.

        Covers Article 17 (Right to Erasure) and Article 30 (Records of Processing).
        """
        report: dict[str, Any] = {
            "framework": "GDPR",
            "regulation": "Regulation (EU) 2016/679",
            "generated_at": end.isoformat(),
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "sections": {},
        }

        # Article 17 — Right to Erasure
        # 0016: the finding is COMPUTED from the erasure-proof service's own stats,
        # never asserted. No service → PARTIAL; no certificates in the period →
        # INSUFFICIENT_DATA; unsigned certificates → PARTIAL; else COMPLIANT.
        ep_stats = self._safe_ep_stats(tenant_id)
        ep_active = self._ep is not None
        certs_total = ep_stats.get("total", 0) or 0
        certs_signed = ep_stats.get("signed_count", 0) or 0
        if not ep_active:
            a17_finding = "PARTIAL"
        elif certs_total == 0:
            a17_finding = "INSUFFICIENT_DATA"
        elif certs_signed < certs_total:
            a17_finding = "PARTIAL"
        else:
            a17_finding = "COMPLIANT"
        report["sections"]["article_17_erasure"] = {
            "title": "Article 17 — Right to Erasure",
            "requirement": "Data subjects have the right to obtain erasure of personal "
                          "data without undue delay.",
            "compliance_evidence": {
                "erasure_proof_active": ep_active,
                "certificates_issued": certs_total,
                "decisions_scrubbed": ep_stats.get("total_scrubbed", 0),
                "signed_certificates": certs_signed,
                "signature_algorithm": "Ed25519",
                "content_hash_algorithm": "BLAKE2b-128 of the pre-delete content "
                                          "(server-observed on the delete endpoint; "
                                          "caller-asserted on /v1/erasure/issue)",
                "first_erasure": ep_stats.get("first_erasure"),
                "last_erasure": ep_stats.get("last_erasure"),
            },
            "evidence_basis": {
                "observed": ["erasure_proof_active", "certificates_issued",
                             "signed_certificates", "decisions_scrubbed"],
                "declared": ["signature_algorithm", "content_hash_algorithm"],
            },
            "finding": a17_finding,
        }

        # Article 30 — Records of Processing Activities
        dt_stats = self._safe_dt_stats(tenant_id)
        report["sections"]["article_30_records"] = {
            "title": "Article 30 — Records of Processing Activities",
            "requirement": "Maintain a record of processing activities under its responsibility.",
            "compliance_evidence": {
                "decision_trail_active": dt_stats.get("total", 0) > 0,
                "total_processing_records": dt_stats.get("total", 0),
                "models_used": dt_stats.get("models_used"),
                "replay_available": True,
                "export_format": "NDJSON",
                "retention_policy": "Configurable per tenant",
            },
            "finding": "COMPLIANT" if dt_stats.get("total", 0) > 0 else "INSUFFICIENT_DATA",
        }

        # Article 25 — Data Protection by Design
        # 0016: computed from what is actually configured for this deployment/tenant.
        # Design facts that cannot be measured at runtime are labelled "declared".
        signing_ok = self._signing_configured()
        active_types = self._active_policy_types(tenant_id)
        report["sections"]["article_25_by_design"] = {
            "title": "Article 25 — Data Protection by Design and by Default",
            "requirement": "Implement appropriate technical measures for data protection.",
            "compliance_evidence": {
                "content_addressing": "BLAKE2b-128 for facts, BLAKE2b-256 for corpus",
                "cryptographic_signing": "Ed25519 for decisions and erasure certificates",
                "cryptographic_signing_configured": signing_ok,
                # Logical residency: rows are tenant-scoped in shared storage. This is
                # NOT physical segregation and is not described as such.
                "tenant_isolation": "Logical per-tenant residency (tenant-scoped storage); "
                                    "not physical segregation",
                "pii_guard_available": "pii_guard" in active_types,
                "gdpr_scrub_endpoint": "DELETE /v1/decisions/scrub/{fact_ref}",
                "erasure_certificates": "POST /v1/stores/{store_id}/delete (observed digest); "
                                        "POST /v1/erasure/issue (already-deleted facts)",
                "erasure_certificates_available": ep_active,
            },
            "evidence_basis": {
                "observed": ["cryptographic_signing_configured", "pii_guard_available",
                             "erasure_certificates_available"],
                "declared": ["content_addressing", "cryptographic_signing",
                             "tenant_isolation", "gdpr_scrub_endpoint",
                             "erasure_certificates"],
            },
            "finding": "COMPLIANT" if (signing_ok and ep_active) else "PARTIAL",
        }

        report["overall_finding"] = self._overall(report["sections"])
        return report

    def _gen_dora(
        self, tenant_id: str, start: datetime, end: datetime,
    ) -> dict[str, Any]:
        """DORA (Digital Operational Resilience Act) compliance report.

        Covers ICT risk management and incident reporting.
        """
        report: dict[str, Any] = {
            "framework": "DORA",
            "regulation": "Regulation (EU) 2022/2554",
            "generated_at": end.isoformat(),
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "sections": {},
        }

        # Article 6 — ICT Risk Management Framework
        gov_stats = self._safe_gov_stats(tenant_id)
        report["sections"]["article_6_ict_risk"] = {
            "title": "Article 6 — ICT Risk Management Framework",
            "requirement": "Establish a sound ICT risk management framework.",
            "compliance_evidence": {
                "governance_gateway_active": gov_stats.get("policies_active", 0) > 0,
                "policy_types_available": [
                    "rate_limit", "model_allowlist", "content_filter",
                    "data_scope", "token_budget", "hitl_required", "pii_guard",
                ],
                "active_policies": gov_stats.get("policies_active", 0),
                "total_evaluations": gov_stats.get("evaluations_total", 0),
                "blocked_requests": gov_stats.get("evaluations_denied", 0),
            },
            "finding": "COMPLIANT" if gov_stats.get("policies_active", 0) > 0 else "PARTIAL",
        }

        # Article 9 — Protection and Prevention
        # 0016: rate limiting, PII detection and content filtering exist as governance
        # POLICY TYPES. The evidence is whether a policy of each type is ACTIVE for this
        # tenant — and the scope note states where those policies are applied. They are
        # never asserted True by default.
        active_types = self._active_policy_types(tenant_id)
        signing_ok = self._signing_configured()
        rate_ok = "rate_limit" in active_types
        pii_ok = "pii_guard" in active_types
        filter_ok = "content_filter" in active_types
        report["sections"]["article_9_protection"] = {
            "title": "Article 9 — Protection and Prevention",
            "requirement": "Implement policies, procedures, and tools for ICT security.",
            "compliance_evidence": {
                "content_integrity": "BLAKE2b content addressing",
                "signature_verification": "Ed25519 for decisions and certificates",
                "signature_verification_configured": signing_ok,
                "rate_limiting": rate_ok,
                "pii_detection": pii_ok,
                "content_filtering": filter_ok,
                "policy_enforcement_scope": "governance policies are evaluated on requests "
                                            "submitted to /v1/governance/evaluate and by the "
                                            "orchestrator; they are not applied inline to "
                                            "memory operations",
                "access_control": "API key scopes + logical per-tenant residency",
            },
            "evidence_basis": {
                "observed": ["signature_verification_configured", "rate_limiting",
                             "pii_detection", "content_filtering"],
                "declared": ["content_integrity", "signature_verification",
                             "policy_enforcement_scope", "access_control"],
            },
            "finding": "COMPLIANT" if (signing_ok and rate_ok and pii_ok and filter_ok)
                       else "PARTIAL",
        }

        # Article 11 — Audit Trail
        dt_stats = self._safe_dt_stats(tenant_id)
        report["sections"]["article_11_audit_trail"] = {
            "title": "Article 11 — Response and Recovery / Audit Trail",
            "requirement": "Maintain audit trails for all ICT-related incidents.",
            "compliance_evidence": {
                "decision_trail_active": dt_stats.get("total", 0) > 0,
                "total_decisions_logged": dt_stats.get("total", 0),
                "governance_evaluation_log": gov_stats.get("evaluations_total", 0),
                "erasure_audit_trail": True,
                "all_records_immutable": "Content-addressed with BLAKE2b",
            },
            "finding": "COMPLIANT" if dt_stats.get("total", 0) > 0 else "PARTIAL",
        }

        report["overall_finding"] = self._overall(report["sections"])
        return report

    def _gen_full_audit(
        self, tenant_id: str, start: datetime, end: datetime,
    ) -> dict[str, Any]:
        """Full audit package — combines all frameworks."""
        eu_ai = self._gen_eu_ai_act(tenant_id, start, end)
        gdpr = self._gen_gdpr(tenant_id, start, end)
        dora = self._gen_dora(tenant_id, start, end)

        findings = [
            eu_ai["overall_finding"],
            gdpr["overall_finding"],
            dora["overall_finding"],
        ]

        return {
            "framework": "Full Audit Package",
            "generated_at": end.isoformat(),
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "frameworks": {
                "eu_ai_act": eu_ai,
                "gdpr": gdpr,
                "dora": dora,
            },
            "overall_finding": "COMPLIANT" if all(f == "COMPLIANT" for f in findings) else "PARTIAL",
            "framework_summary": {
                "eu_ai_act": eu_ai["overall_finding"],
                "gdpr": gdpr["overall_finding"],
                "dora": dora["overall_finding"],
            },
        }

    # ------------------------------------------------------------------
    # Safe data fetchers
    # ------------------------------------------------------------------

    def _safe_dt_stats(self, tenant_id: str) -> dict:
        if self._dt is None:
            return {}
        try:
            return self._dt.get_stats(tenant_id)
        except Exception:
            return {}

    def _safe_ep_stats(self, tenant_id: str) -> dict:
        if self._ep is None:
            return {}
        try:
            return self._ep.get_stats(tenant_id)
        except Exception:
            return {}

    def _safe_gov_stats(self, tenant_id: str) -> dict:
        if self._gov is None:
            return {}
        try:
            return self._gov.get_stats(tenant_id)
        except Exception:
            return {}

    def _active_policy_types(self, tenant_id: str) -> set[str]:
        """Policy types with at least one ENABLED policy for the tenant (0016).

        Observed, not declared: read from the governance gateway. No gateway, or a
        read failure, yields the empty set — never a default of "everything on".
        """
        if self._gov is None:
            return set()
        try:
            policies = self._gov.list_policies(tenant_id, enabled_only=True)
        except Exception:
            return set()
        types: set[str] = set()
        for p in policies:
            pt = getattr(p, "policy_type", None)
            if pt is None and isinstance(p, dict):
                pt = p.get("policy_type")
            if pt is not None:
                types.add(getattr(pt, "value", pt))
        return types

    def _signing_configured(self) -> bool:
        """True only if the erasure-proof service exists AND can sign right now (0016)."""
        if self._ep is None:
            return False
        try:
            self._ep.assert_can_sign()
            return True
        except Exception:
            return False

    def _safe_comp_data(self, tenant_id: str) -> dict:
        if self._comp is None:
            return {}
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT conformance_rate, capabilities FROM compliance_records "
                "WHERE tenant_id = %s ORDER BY created_at DESC LIMIT 1",
                (tenant_id,),
            ).fetchone()
            if row:
                caps = row.get("capabilities")
                if isinstance(caps, str):
                    caps = json.loads(caps)
                return {
                    "conformance_rate": row.get("conformance_rate"),
                    "capabilities": caps or [],
                }
        except Exception:
            pass
        return {}

    def _count_hitl_policies(self, tenant_id: str) -> int:
        if self._gov is None:
            return 0
        try:
            policies = self._gov.list_policies(tenant_id, enabled_only=True)
            return sum(1 for p in policies if p.policy_type.value == "hitl_required")
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _overall(sections: dict) -> str:
        findings = [s.get("finding", "UNKNOWN") for s in sections.values()]
        if all(f == "COMPLIANT" for f in findings):
            return "COMPLIANT"
        if any(f == "INSUFFICIENT_DATA" for f in findings):
            return "INSUFFICIENT_DATA"
        return "PARTIAL"

    @staticmethod
    def _title_for(rt: ReportType, start: datetime, end: datetime) -> str:
        names = {
            ReportType.EU_AI_ACT: "EU AI Act Compliance Report",
            ReportType.GDPR: "GDPR Compliance Report",
            ReportType.DORA: "DORA Compliance Report",
            ReportType.FULL_AUDIT: "Full Regulatory Audit Package",
        }
        s = start.strftime("%Y-%m-%d")
        e = end.strftime("%Y-%m-%d")
        return f"{names.get(rt, 'Report')} ({s} → {e})"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self, report: Report) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO regulatory_reports "
            "(report_id, tenant_id, report_type, title, status, "
            " period_start, period_end, content, content_hash, "
            " created_at, file_size_bytes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                report.report_id, report.tenant_id, report.report_type.value,
                report.title, report.status.value,
                report.period_start, report.period_end,
                json.dumps(report.content, default=str), report.content_hash,
                report.created_at, report.file_size_bytes,
            ),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, report_id: str) -> Report | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM regulatory_reports WHERE report_id = %s",
            (report_id,),
        ).fetchone()
        return self._row_to_report(row) if row else None

    def list_reports(
        self, tenant_id: str, limit: int = 20, offset: int = 0,
    ) -> list[Report]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM regulatory_reports "
            "WHERE tenant_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (tenant_id, limit, offset),
        ).fetchall()
        return [self._row_to_report(r) for r in rows]

    def get_stats(self, tenant_id: str) -> dict[str, Any]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "  COUNT(CASE WHEN status = 'complete' THEN 1 END) AS complete, "
            "  MAX(created_at) AS last_report "
            "FROM regulatory_reports WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchone()
        return {
            "total": row["total"] if row else 0,
            "complete": row["complete"] if row else 0,
            "last_report": row["last_report"].isoformat() if row and row["last_report"] else None,
        }

    def delete(self, report_id: str, tenant_id: str) -> bool:
        conn = self._get_conn()
        result = conn.execute(
            "DELETE FROM regulatory_reports "
            "WHERE report_id = %s AND tenant_id = %s",
            (report_id, tenant_id),
        )
        conn.commit()
        return result.rowcount > 0

    # ------------------------------------------------------------------
    # Row converter
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_report(row: dict[str, Any]) -> Report:
        content = row.get("content")
        if isinstance(content, str):
            content = json.loads(content)
        elif content is None:
            content = {}

        return Report(
            report_id=row["report_id"],
            tenant_id=row["tenant_id"],
            report_type=ReportType(row["report_type"]),
            title=row["title"],
            status=ReportStatus(row["status"]),
            period_start=row["period_start"],
            period_end=row["period_end"],
            content=content,
            content_hash=row.get("content_hash"),
            created_at=row["created_at"],
            file_size_bytes=row.get("file_size_bytes", 0),
        )

    @staticmethod
    def report_to_dict(r: Report) -> dict[str, Any]:
        return {
            "report_id": r.report_id,
            "tenant_id": r.tenant_id,
            "report_type": r.report_type.value,
            "title": r.title,
            "status": r.status.value,
            "period_start": r.period_start.isoformat(),
            "period_end": r.period_end.isoformat(),
            "content": r.content,
            "content_hash": r.content_hash,
            "created_at": r.created_at.isoformat(),
            "file_size_bytes": r.file_size_bytes,
        }

    @staticmethod
    def report_summary(r: Report) -> dict[str, Any]:
        """Lightweight summary (no content) for list views."""
        return {
            "report_id": r.report_id,
            "report_type": r.report_type.value,
            "title": r.title,
            "status": r.status.value,
            "period_start": r.period_start.isoformat(),
            "period_end": r.period_end.isoformat(),
            "content_hash": r.content_hash,
            "created_at": r.created_at.isoformat(),
            "file_size_bytes": r.file_size_bytes,
            "overall_finding": r.content.get("overall_finding"),
        }
