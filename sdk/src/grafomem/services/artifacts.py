"""
sdk/src/grafomem/services/artifacts.py   (R1 — mirror services/landing.py)

ADAPT: align .post/.get to HTTPTransport's real method names (see services/erasure.py).
"""


class ArtifactsService:
    def __init__(self, http):
        self._http = http

    def register(self, *, artifact_ref, base_model_ref, layers, kind="lora+rag", metadata=None):
        return self._http.post("/v1/artifacts/register", json={
            "artifact_ref": artifact_ref, "base_model_ref": base_model_ref,
            "layers": layers, "kind": kind, "metadata": metadata or {}})

    def get(self, artifact_id):
        return self._http.get(f"/v1/artifacts/{artifact_id}")

    def verify(self, artifact_id):
        return self._http.get(f"/v1/artifacts/{artifact_id}/verify")

    def integrity(self, artifact_id, layer_hashes):
        return self._http.post(f"/v1/artifacts/{artifact_id}/integrity", json={"layer_hashes": layer_hashes})

    def certify(self, artifact_id, certificate_id):
        return self._http.post(f"/v1/artifacts/{artifact_id}/certify", json={"certificate_id": certificate_id})

    def list(self, limit=50, offset=0):
        return self._http.get("/v1/artifacts", params={"limit": limit, "offset": offset})

    def stats(self):
        return self._http.get("/v1/artifacts/stats")

    def approve(self, artifact_id, approver):
        return self._http.post(f"/v1/artifacts/{artifact_id}/approve", json={"approver": approver})

    def reject(self, artifact_id, approver):
        return self._http.post(f"/v1/artifacts/{artifact_id}/reject", json={"approver": approver})
