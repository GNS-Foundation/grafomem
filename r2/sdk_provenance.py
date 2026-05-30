"""
sdk/src/grafomem/services/provenance.py   (R2 — mirror services/landing.py)

ADAPT: align .post/.get to HTTPTransport's real method names (see services/erasure.py).
"""


class ProvenanceService:
    def __init__(self, http):
        self._http = http

    def register_corpus(self, *, name, sources, attestations=None, processing=None, metadata=None):
        return self._http.post("/v1/provenance/corpora", json={
            "name": name, "sources": sources, "attestations": attestations or {},
            "processing": processing or [], "metadata": metadata or {}})

    def get_corpus(self, corpus_id):
        return self._http.get(f"/v1/provenance/corpora/{corpus_id}")

    def verify(self, corpus_id):
        return self._http.get(f"/v1/provenance/corpora/{corpus_id}/verify")

    def inclusion_proof(self, corpus_id, source_id):
        return self._http.get(f"/v1/provenance/corpora/{corpus_id}/proof", params={"source_id": source_id})

    def provenance_block(self, corpus_id):
        return self._http.get(f"/v1/provenance/corpora/{corpus_id}/block")

    def list(self, limit=50, offset=0):
        return self._http.get("/v1/provenance/corpora", params={"limit": limit, "offset": offset})

    def stats(self):
        return self._http.get("/v1/provenance/stats")
