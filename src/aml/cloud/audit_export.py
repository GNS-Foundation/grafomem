"""
GRAFOMEM Audit Export Service — Sprint 23.

Produces compliance-ready exports of governance logs, decision trail,
and gcrumbs chain in CSV, JSON, and PDF formats.  Each export includes
a BLAKE2b-256 content hash and Ed25519 signature for tamper detection.

Backed by the existing DecisionTrail, GovernanceGateway, and GcrumbsService.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("grafomem.cloud.audit_export")


# ============================================================================
# Data types
# ============================================================================

@dataclass(slots=True)
class ExportMetadata:
    """Metadata embedded in every export file."""
    tenant_id: str
    export_type: str  # "decisions" | "governance" | "gcrumbs" | "full"
    format: str       # "csv" | "json" | "pdf" | "zip"
    record_count: int
    date_from: str | None
    date_to: str | None
    content_hash: str  # BLAKE2b-256 hex
    signature: str | None  # Ed25519 hex (if signing key available)
    exported_at: str


@dataclass(slots=True)
class ExportResult:
    """Container for an export's content + metadata."""
    data: bytes
    metadata: ExportMetadata
    content_type: str
    filename: str


# ============================================================================
# AuditExportService
# ============================================================================

class AuditExportService:
    """Produces compliance-ready bulk exports.

    Parameters
    ----------
    decision_trail : object
        The DecisionTrail service instance.
    governance : object
        The GovernanceGateway service instance.
    gcrumbs : object | None
        The GcrumbsService instance (if available).
    signing_key : bytes | None
        Ed25519 private key bytes for signing exports.
    """

    def __init__(
        self,
        decision_trail: Any,
        governance: Any,
        gcrumbs: Any | None = None,
        signing_key: bytes | None = None,
    ) -> None:
        self._decisions = decision_trail
        self._governance = governance
        self._gcrumbs = gcrumbs
        self._signing_key = signing_key

    # ------------------------------------------------------------------
    # Decision export
    # ------------------------------------------------------------------

    def export_decisions(
        self,
        tenant_id: str,
        *,
        format: str = "json",
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> ExportResult:
        """Export all decisions for a tenant."""
        records = self._decisions.export(tenant_id)

        # Filter by date range
        if date_from or date_to:
            records = self._filter_by_date(
                records, "created_at", date_from, date_to,
            )

        if format == "csv":
            data = self._to_csv(records, [
                "decision_id", "store_id", "query", "model_id",
                "raw_output", "output_tokens", "latency_ms",
                "created_at",
            ])
            ct = "text/csv"
            ext = "csv"
        else:
            data = self._to_json(records)
            ct = "application/json"
            ext = "json"

        return self._wrap(data, tenant_id, "decisions", ext, len(records),
                          date_from, date_to, ct)

    # ------------------------------------------------------------------
    # Governance log export
    # ------------------------------------------------------------------

    def export_governance(
        self,
        tenant_id: str,
        *,
        format: str = "json",
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> ExportResult:
        """Export governance evaluation logs for a tenant."""
        logs = self._governance.get_evaluation_logs(tenant_id)

        if date_from or date_to:
            logs = self._filter_by_date(
                logs, "evaluated_at", date_from, date_to,
            )

        if format == "csv":
            data = self._to_csv(logs, [
                "log_id", "policy_name", "policy_type", "operation",
                "result", "detail", "evaluated_at",
            ])
            ct = "text/csv"
            ext = "csv"
        else:
            data = self._to_json(logs)
            ct = "application/json"
            ext = "json"

        return self._wrap(data, tenant_id, "governance", ext, len(logs),
                          date_from, date_to, ct)

    # ------------------------------------------------------------------
    # Gcrumbs chain export
    # ------------------------------------------------------------------

    def export_gcrumbs(
        self,
        tenant_id: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> ExportResult:
        """Export the full gcrumbs breadcrumb chain + epoch summary."""
        if not self._gcrumbs:
            return self._wrap(
                b"[]", tenant_id, "gcrumbs", "json", 0,
                date_from, date_to, "application/json",
            )

        breadcrumbs = self._gcrumbs.list_breadcrumbs(tenant_id)
        epochs = self._gcrumbs.list_epochs(tenant_id)

        export_data = {
            "breadcrumbs": breadcrumbs,
            "epochs": epochs,
            "chain_length": len(breadcrumbs),
            "epoch_count": len(epochs),
        }

        data = json.dumps(export_data, indent=2, default=str).encode("utf-8")
        return self._wrap(data, tenant_id, "gcrumbs", "json",
                          len(breadcrumbs), date_from, date_to,
                          "application/json")

    # ------------------------------------------------------------------
    # Full audit package (ZIP)
    # ------------------------------------------------------------------

    def export_full(
        self,
        tenant_id: str,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> ExportResult:
        """Export a full audit package as a ZIP file.

        Contains:
        - decisions.json
        - governance.json
        - gcrumbs.json
        - manifest.json (metadata + hashes)
        """
        decisions = self.export_decisions(
            tenant_id, format="json",
            date_from=date_from, date_to=date_to,
        )
        governance = self.export_governance(
            tenant_id, format="json",
            date_from=date_from, date_to=date_to,
        )
        gcrumbs = self.export_gcrumbs(
            tenant_id, date_from=date_from, date_to=date_to,
        )

        # Build ZIP
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("decisions.json", decisions.data)
            zf.writestr("governance.json", governance.data)
            zf.writestr("gcrumbs.json", gcrumbs.data)

            manifest = {
                "tenant_id": tenant_id,
                "exported_at": datetime.now(tz=timezone.utc).isoformat(),
                "date_from": date_from,
                "date_to": date_to,
                "files": {
                    "decisions.json": {
                        "records": decisions.metadata.record_count,
                        "hash": decisions.metadata.content_hash,
                    },
                    "governance.json": {
                        "records": governance.metadata.record_count,
                        "hash": governance.metadata.content_hash,
                    },
                    "gcrumbs.json": {
                        "records": gcrumbs.metadata.record_count,
                        "hash": gcrumbs.metadata.content_hash,
                    },
                },
            }
            zf.writestr("manifest.json",
                         json.dumps(manifest, indent=2))

        zip_data = buf.getvalue()
        total_records = (
            decisions.metadata.record_count +
            governance.metadata.record_count +
            gcrumbs.metadata.record_count
        )
        return self._wrap(zip_data, tenant_id, "full", "zip",
                          total_records, date_from, date_to,
                          "application/zip")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _wrap(
        self,
        data: bytes,
        tenant_id: str,
        export_type: str,
        ext: str,
        record_count: int,
        date_from: str | None,
        date_to: str | None,
        content_type: str,
    ) -> ExportResult:
        """Wrap raw data with hash, signature, and metadata."""
        content_hash = hashlib.blake2b(data, digest_size=32).hexdigest()

        signature = None
        if self._signing_key:
            try:
                from nacl.signing import SigningKey
                sk = SigningKey(self._signing_key)
                sig = sk.sign(bytes.fromhex(content_hash))
                signature = sig.signature.hex()
            except Exception as e:
                logger.warning("Export signing failed: %s", e)

        now = datetime.now(tz=timezone.utc).isoformat()
        ts = now.replace(":", "").replace("-", "")[:15]

        metadata = ExportMetadata(
            tenant_id=tenant_id,
            export_type=export_type,
            format=ext,
            record_count=record_count,
            date_from=date_from,
            date_to=date_to,
            content_hash=content_hash,
            signature=signature,
            exported_at=now,
        )

        filename = f"grafomem_audit_{export_type}_{ts}.{ext}"

        return ExportResult(
            data=data,
            metadata=metadata,
            content_type=content_type,
            filename=filename,
        )

    @staticmethod
    def _to_csv(records: list[dict], columns: list[str]) -> bytes:
        """Convert a list of dicts to CSV bytes."""
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            # Flatten any nested dicts/lists to JSON strings
            row = {}
            for col in columns:
                val = record.get(col, "")
                if isinstance(val, (dict, list)):
                    val = json.dumps(val)
                row[col] = val
            writer.writerow(row)
        return buf.getvalue().encode("utf-8")

    @staticmethod
    def _to_json(records: list[dict]) -> bytes:
        """Convert a list of dicts to JSON bytes."""
        return json.dumps(records, indent=2, default=str).encode("utf-8")

    @staticmethod
    def _filter_by_date(
        records: list[dict],
        date_field: str,
        date_from: str | None,
        date_to: str | None,
    ) -> list[dict]:
        """Filter records by date range."""
        filtered = []
        for r in records:
            ts_str = r.get(date_field, "")
            if not ts_str:
                filtered.append(r)
                continue
            try:
                ts = ts_str if isinstance(ts_str, str) else str(ts_str)
                if date_from and ts < date_from:
                    continue
                if date_to and ts > date_to:
                    continue
            except (TypeError, ValueError):
                pass
            filtered.append(r)
        return filtered
