"""
sdk/src/grafomem/services/landing.py   (PHASE-B — mirror services/erasure.py)

Constructor takes the shared HTTPTransport, exactly like ErasureService(self._http).
ADAPT: align the .post/.get calls to HTTPTransport's real method names (open
services/erasure.py and copy its transport-call style).
"""
from typing import Any


class LandingService:
    def __init__(self, http):
        self._http = http   # grafomem._http.HTTPTransport

    def run_conformance(self, artifact_ref: str, layer_hashes: list, data_provenance: dict) -> dict:
        return self._http.post("/v1/landing/conformance", json={
            "artifact_ref": artifact_ref, "layer_hashes": layer_hashes, "data_provenance": data_provenance})

    def issue(self, *, artifact_ref: str, base_model_ref: str, layer_hashes: list,
              data_provenance: dict, authority: dict, conformance: dict,
              permitted_actions: list, kind: str = "lora+rag") -> dict:
        return self._http.post("/v1/landing/issue", json={
            "artifact_ref": artifact_ref, "base_model_ref": base_model_ref, "layer_hashes": layer_hashes,
            "data_provenance": data_provenance, "authority": authority, "conformance": conformance,
            "permitted_actions": permitted_actions, "kind": kind})

    def get(self, certificate_id: str) -> dict:
        return self._http.get(f"/v1/landing/{certificate_id}")

    def verify(self, certificate_id: str) -> dict:
        return self._http.get(f"/v1/landing/{certificate_id}/verify")

    def list(self, limit: int = 50, offset: int = 0) -> list:
        return self._http.get("/v1/landing", params={"limit": limit, "offset": offset})

    def stats(self) -> dict:
        return self._http.get("/v1/landing/stats")

    def approve(self, certificate_id: str, approver: str) -> dict:
        return self._http.post(f"/v1/landing/{certificate_id}/approve", json={"approver": approver})

    def reject(self, certificate_id: str, approver: str) -> dict:
        return self._http.post(f"/v1/landing/{certificate_id}/reject", json={"approver": approver})
