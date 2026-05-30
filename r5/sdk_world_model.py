"""
sdk/src/grafomem/services/world_model.py   (R5 — mirror services/landing.py)

ADAPT: align .post/.get to HTTPTransport's real method names (see services/erasure.py).
"""


class WorldModelService:
    def __init__(self, http):
        self._http = http

    # types
    def register_type(self, *, kind, name, spec):
        return self._http.post("/v1/world-model/types", json={"kind": kind, "name": name, "spec": spec})

    def list_types(self, kind=None):
        return self._http.get("/v1/world-model/types", params={"kind": kind} if kind else None)

    def get_type(self, type_id):
        return self._http.get(f"/v1/world-model/types/{type_id}")

    def verify_type(self, type_id):
        return self._http.get(f"/v1/world-model/types/{type_id}/verify")

    # validation
    def validate_object(self, type_name, instance):
        return self._http.post("/v1/world-model/validate/object", json={"type_name": type_name, "instance": instance})

    def validate_link(self, link_name, from_type, to_type):
        return self._http.post("/v1/world-model/validate/link",
                               json={"link_name": link_name, "from_type": from_type, "to_type": to_type})

    # governed actions
    def invoke(self, *, action_name, subject_refs, params=None, authority=None):
        return self._http.post("/v1/world-model/actions/invoke", json={
            "action_name": action_name, "subject_refs": subject_refs,
            "params": params or {}, "authority": authority or {}})

    def get_action(self, action_id):
        return self._http.get(f"/v1/world-model/actions/{action_id}")

    def verify_action(self, action_id):
        return self._http.get(f"/v1/world-model/actions/{action_id}/verify")

    def stats(self):
        return self._http.get("/v1/world-model/actions/stats")

    def approve(self, action_id, approver):
        return self._http.post(f"/v1/world-model/actions/{action_id}/approve", json={"approver": approver})

    def reject(self, action_id, approver):
        return self._http.post(f"/v1/world-model/actions/{action_id}/reject", json={"approver": approver})
