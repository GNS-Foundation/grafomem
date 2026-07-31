"""GRAFOMEM Cloud — official Python client.

A thin, typed wrapper over the GRAFOMEM Cloud REST API for the governed-decision
+ signed-receipt + independent-verification flow.

    from grafomem_cloud import GrafomemClient

    # onboard a tenant (or construct with an existing api_key)
    client, info = GrafomemClient.signup(BASE, name="Acme", email="ops@acme.io", password="…")

    # submit a batch of invoices; verification runs server-side, each result is signed
    out = client.verify_batch(invoices)          # -> {"summary": {...}, "results": [...]}

    # a funder verifies a receipt independently — no api key needed
    key = GrafomemClient(BASE).public_key()["public_key_b64"]
    v = GrafomemClient(BASE).verify([receipt], public_key_b64=key)   # -> {"valid": bool, ...}
"""
from __future__ import annotations

from typing import Any

import httpx

__version__ = "0.1.0"
__all__ = ["GrafomemClient", "GrafomemError"]


class GrafomemError(RuntimeError):
    """Raised on a non-2xx API response; carries status_code and body."""
    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"GRAFOMEM API error {status_code}: {body}")


class GrafomemClient:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        headers = {"X-API-Key": api_key} if api_key else {}
        self._http = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "GrafomemClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _req(self, method: str, path: str, **kw) -> Any:
        r = self._http.request(method, path, **kw)
        if r.status_code // 100 != 2:
            try:
                body = r.json()
            except Exception:
                body = r.text
            raise GrafomemError(r.status_code, body)
        return r.json() if r.content else None

    # -- onboarding --------------------------------------------------------
    @classmethod
    def signup(cls, base_url: str, name: str, email: str, password: str,
               plan: str = "starter", timeout: float = 30.0) -> tuple["GrafomemClient", dict]:
        """Create a tenant; returns (authenticated client, tenant info incl. api_key)."""
        with httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout) as c:
            r = c.post("/v1/portal/signup",
                       json={"name": name, "email": email, "password": password, "plan": plan})
            if r.status_code // 100 != 2:
                raise GrafomemError(r.status_code, r.text)
            info = r.json()
        return cls(base_url, api_key=info["api_key"], timeout=timeout), info

    # -- certification (tenant-scoped; requires api_key) -------------------
    def verify_batch(self, invoices: list[dict], policy: dict | None = None,
                     model_id: str = "kapwork-verify-agent-v1") -> dict:
        """Ingest invoices; the rules engine runs server-side and each result is
        recorded as a signed governed decision + execution receipt."""
        return self._req("POST", "/v1/governed/verify-batch",
                         json={"invoices": invoices, "policy": policy or {}, "model_id": model_id})

    def governed_decision(self, decision: str, reason: str = "", invoice_id: str | None = None,
                          context: dict | None = None,
                          model_id: str = "kapwork-verify-agent-v1") -> dict:
        """Record one externally-made decision as a signed decision_record + receipt."""
        return self._req("POST", "/v1/governed/decisions",
                         json={"decision": decision, "reason": reason, "invoice_id": invoice_id,
                               "context": context or {}, "model_id": model_id})

    def list_decisions(self, limit: int = 100, offset: int = 0) -> dict:
        return self._req("GET", "/v1/decisions/", params={"limit": limit, "offset": offset})

    # -- independent verification (public; no api_key required) ------------
    def public_key(self) -> dict:
        """The signer's Ed25519 public key. Public endpoint — no auth."""
        return self._req("GET", "/v1/gcrumbs/verify/key")

    def verify(self, receipts: list[dict], public_key_b64: str | None = None) -> dict:
        """Stateless verification of one or more receipts against a public key.
        No database access, no auth — this is the funder's check."""
        return self._req("POST", "/v1/gcrumbs/verify",
                         json={"receipts": receipts, "public_key_b64": public_key_b64})

    # -- readiness ---------------------------------------------------------
    def readyz(self) -> dict:
        return self._req("GET", "/readyz")
