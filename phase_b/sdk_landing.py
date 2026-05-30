"""
sdk/src/grafomem/services/landing.py   (PHASE-B SKELETON)

Mirrors services/erasure.py. ADAPT the transport calls to your _http API
(self._http.post/get — confirm method names against sdk/src/grafomem/_http.py).
"""
from typing import Optional


class LandingService:
    def __init__(self, http):
        self._http = http   # ADAPT: the shared httpx transport (with retry/error mapping)

    def run_conformance(self, artifact_ref: str, layer_hashes: list, data_provenance: dict) -> dict:
        return self._http.post("/v1/landing/conformance", json={
            "artifact_ref": artifact_ref, "layer_hashes": layer_hashes,
            "data_provenance": data_provenance})

    def issue(self, *, artifact_ref: str, base_model_ref: str, layer_hashes: list,
              data_provenance: dict, authority: dict, conformance: dict,
              permitted_actions: list, kind: str = "lora+rag") -> dict:
        return self._http.post("/v1/landing/certificates", json={
            "artifact_ref": artifact_ref, "base_model_ref": base_model_ref,
            "layer_hashes": layer_hashes, "data_provenance": data_provenance,
            "authority": authority, "conformance": conformance,
            "permitted_actions": permitted_actions, "kind": kind})

    def get(self, certificate_id: str) -> dict:
        return self._http.get(f"/v1/landing/certificates/{certificate_id}")

    def list(self, limit: int = 50, offset: int = 0) -> list:
        return self._http.get("/v1/landing/certificates", params={"limit": limit, "offset": offset})

    def verify(self, certificate_id: str) -> dict:
        return self._http.post(f"/v1/landing/certificates/{certificate_id}/verify")

    def approve(self, certificate_id: str, approver: str) -> dict:
        return self._http.post(f"/v1/landing/certificates/{certificate_id}/approve", json={"approver": approver})

    def reject(self, certificate_id: str, approver: str) -> dict:
        return self._http.post(f"/v1/landing/certificates/{certificate_id}/reject", json={"approver": approver})
