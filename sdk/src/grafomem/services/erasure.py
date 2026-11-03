"""Erasure proof service.

Provides Ed25519-signed GDPR erasure certificates with independent
verification.

Usage::

    cert = client.erasure.issue(fact_ref=1, reason="GDPR Art 17")
    result = client.erasure.verify(cert.certificate_id)
    assert result.valid
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from grafomem.types import ErasureCertificate, VerificationResult

if TYPE_CHECKING:
    from grafomem._http import HTTPTransport


class ErasureService:
    """GDPR erasure proof — signed deletion certificates."""

    def __init__(self, http: HTTPTransport) -> None:
        self._http = http

    def issue(
        self,
        fact_ref: int,
        *,
        store_id: str = "",
        reason: str = "GDPR Article 17 — Right to Erasure",
        fact_content: str | None = None,
        memory_deleted: bool = True,
        requested_by: str = "data_subject",
    ) -> ErasureCertificate:
        """Issue a signed erasure certificate for a fact that was ALREADY deleted.

        This call does NOT delete the fact. Delete it first with
        ``client.memories.delete(store_id, ref)`` — in Cloud mode that call also
        issues a certificate whose content hash the server observed itself, so
        it is usually all you need. Use this method only to certify a fact that
        was deleted by other means.

        The server probes its stores before issuing. If the fact is still
        present it REFUSES (HTTP 409) — no erasure certificate is minted for a
        fact that was not erased.

        Args:
            fact_ref: The fact reference that was erased (integer).
            store_id: (Unused by API, kept for SDK convenience).
            reason: Legal basis for erasure.
            fact_content: Content to hash (not stored in certificate). Because the
                fact is already gone, this hash is caller-asserted: the server
                cannot check it against the stored record.
            memory_deleted: Deprecated and ignored by the server; kept so existing
                callers do not break.
            requested_by: Who requested the erasure.

        Returns:
            An :class:`ErasureCertificate` with signature and hash.

        Raises:
            An HTTP 409 error from the API if the fact is still present.
        """
        body: dict[str, Any] = {
            "fact_ref": int(fact_ref),
            "legal_basis": reason,
            "requested_by": requested_by,
        }
        if fact_content:
            body["fact_content"] = fact_content

        data = self._http.post("/v1/erasure/issue", json=body)
        return ErasureCertificate.model_validate(data)

    def verify(self, certificate_id: str) -> VerificationResult:
        """Verify an erasure certificate's Ed25519 signature.

        Args:
            certificate_id: The certificate ID.

        Returns:
            A :class:`VerificationResult` with ``valid`` and ``detail``.
        """
        data = self._http.get(f"/v1/erasure/{certificate_id}/verify")
        return VerificationResult.model_validate(data)

    def get(self, certificate_id: str) -> ErasureCertificate:
        """Get an erasure certificate by ID."""
        data = self._http.get(f"/v1/erasure/{certificate_id}")
        return ErasureCertificate.model_validate(data)

    def list(self) -> list[ErasureCertificate]:
        """List all erasure certificates."""
        data = self._http.get("/v1/erasure/")
        items = data.get("certificates", []) if isinstance(data, dict) else data
        return [ErasureCertificate.model_validate(c) for c in items]

    def find_by_fact(self, fact_ref: int) -> ErasureCertificate:
        """Find an erasure certificate by fact reference."""
        data = self._http.get(f"/v1/erasure/fact/{fact_ref}")
        return ErasureCertificate.model_validate(data)

    def stats(self) -> dict[str, Any]:
        """Get erasure proof summary statistics."""
        data = self._http.get("/v1/erasure/stats")
        return data or {}
