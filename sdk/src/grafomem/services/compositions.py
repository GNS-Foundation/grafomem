"""
sdk/src/grafomem/services/compositions.py   (R4 — mirror services/landing.py)

ADAPT: align .post/.get to HTTPTransport's real method names (see services/erasure.py).
"""


class CompositionsService:
    def __init__(self, http):
        self._http = http

    def compose(self, *, composition_kind, members, target_ref, authority=None, required_trust_tier="verified"):
        return self._http.post("/v1/compositions", json={
            "composition_kind": composition_kind, "members": members, "target_ref": target_ref,
            "authority": authority or {}, "required_trust_tier": required_trust_tier})

    def get(self, composition_id):
        return self._http.get(f"/v1/compositions/{composition_id}")

    def verify(self, composition_id):
        return self._http.get(f"/v1/compositions/{composition_id}/verify")

    def composed_artifact(self, composition_id):
        return self._http.get(f"/v1/compositions/{composition_id}/artifact")

    def list(self, limit=50, offset=0):
        return self._http.get("/v1/compositions", params={"limit": limit, "offset": offset})

    def stats(self):
        return self._http.get("/v1/compositions/stats")

    def approve(self, composition_id, approver):
        return self._http.post(f"/v1/compositions/{composition_id}/approve", json={"approver": approver})

    def reject(self, composition_id, approver):
        return self._http.post(f"/v1/compositions/{composition_id}/reject", json={"approver": approver})
