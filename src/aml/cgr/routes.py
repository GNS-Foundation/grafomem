"""CGR scoring + issuance HTTP surface (read-only). Guarded by the `cgr:read` scope.

  GET /v1/cgr/scores                    → {scores: [CGRResult...], as_of, dimension}
  GET /v1/cgr/scores/{handle}           → {score: CGRResult, tiergate: {...}}
  GET /v1/cgr/attestation/{handle}      → Foundation-signed CGRAttestation
  GET /v1/cgr/attestations              → bulk list of signed attestations
  GET /v1/cgr/issuer                    → {issuer, issuer_key_id, public_key, schema}

Thin web adapter: the only cgr module that imports fastapi + the scope helper.
All scoring lives in aml.cgr.engine; the attestation logic is pure in
aml.cgr.attestation with crypto injected from aml.cgr.issuance. Dependencies
(decision_trail, store_manager, foundation_identity, gcrumbs) are injected by the
app factory.
"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from aml.cgr.attestation import (
    CGR_ATTESTATION_SCHEMA,
    attestation_fingerprint,
    build_attestation,
)
from aml.cgr.engine import compute_scores, to_tiergate
from aml.cgr.issuance import ISSUER, issuer_key_id, make_signer
from aml.cgr.scoring import DIMENSION_RECEIVABLES, _now_iso


def _tenant_id(request: Request) -> str:
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(401, "Authentication required")
    return ctx.tenant_id


def create_cgr_scoring_router(decision_trail, store_manager) -> APIRouter:
    from aml.server.scopes import require_scope

    router = APIRouter(prefix="/v1/cgr", tags=["CGR Scores"])

    def _guard():
        if decision_trail is None or store_manager is None:
            raise HTTPException(503, "CGR scoring services not available")

    @router.get("/scores")
    async def list_scores(request: Request, limit: int = 500, offset: int = 0):
        tenant_id = _tenant_id(request)
        require_scope(request, "cgr:read")
        _guard()
        results = compute_scores(decision_trail, store_manager, tenant_id, limit=limit, offset=offset)
        return {
            "scores": [asdict(r) for r in results],
            "count": len(results),
            "as_of": results[0].as_of if results else _now_iso(),
            "dimension": DIMENSION_RECEIVABLES,
        }

    @router.get("/scores/{agent_handle:path}")
    async def get_score(agent_handle: str, request: Request, limit: int = 500, offset: int = 0):
        tenant_id = _tenant_id(request)
        require_scope(request, "cgr:read")
        _guard()
        results = compute_scores(decision_trail, store_manager, tenant_id, limit=limit, offset=offset)
        match = next((r for r in results if r.agent_handle == agent_handle), None)
        if match is None:
            raise HTTPException(404, f"No CGR score for agent_handle {agent_handle!r}")
        return {"score": asdict(match), "tiergate": to_tiergate(match)}

    return router


def create_cgr_issuance_router(
    decision_trail, store_manager, foundation_identity, *, gcrumbs=None
) -> APIRouter:
    """Foundation-issuance seam. Emits Foundation-signed CGRAttestations wrapping
    `to_tiergate`. The Foundation identity is DISTINCT from the commercial
    signing_identity (that's the whole neutrality invariant). If the Foundation
    seed is absent, `foundation_identity is None` and every endpoint returns 503 —
    never falling back to the commercial key.
    """
    from aml.server.scopes import require_scope

    router = APIRouter(prefix="/v1/cgr", tags=["CGR Issuance"])

    def _foundation():
        if foundation_identity is None:
            raise HTTPException(503, "CGR Foundation issuer not available (FOUNDATION_SIGNING_SEED unset)")
        return foundation_identity

    def _scoring_guard():
        if decision_trail is None or store_manager is None:
            raise HTTPException(503, "CGR scoring services not available")

    def _issue(tenant_id: str, result) -> dict:
        """Score result -> to_tiergate -> Foundation-signed attestation, anchored
        into gcrumbs (best-effort) with the attestation fingerprint."""
        identity = _foundation()
        signer = make_signer(identity)
        kid = issuer_key_id(identity)
        tiergate = to_tiergate(result)
        att = build_attestation(tiergate, signer=signer, issuer_key_id=kid, evidence_ref=None)
        att["evidence_ref"] = _anchor(tenant_id, tiergate, att)
        return att

    def _anchor(tenant_id: str, tiergate: dict, att: dict):
        """Anchor the attestation fingerprint into the gcrumbs chain. Returns the
        breadcrumb_id (evidence_ref) or None if gcrumbs is unavailable / errors —
        gcrumbs is the audit anchor, not a hard dependency for the POC."""
        if gcrumbs is None:
            return None
        try:
            payload = {
                "agent_handle": tiergate["agent_handle"],
                "band": tiergate["tier"],
                "cgr_score": tiergate["cgr_score"],
                "as_of": tiergate["as_of"],
                "schema": CGR_ATTESTATION_SCHEMA,
                "attestation_fingerprint": attestation_fingerprint(att),
                "signature": att["signature"],
            }
            bc = gcrumbs.append_breadcrumb(tenant_id, "cgr:attestation:issued", payload)
            return bc.get("breadcrumb_id")
        except Exception:
            return None

    # ── Gate-1 calibration authority (privileged WRITE) ──────────────────────
    def _calib_conn():
        pool = getattr(decision_trail, "_pool", None)
        if pool is None:
            raise HTTPException(503, "CGR scoring services not available")
        return pool

    def _require_calibration_authority(pool, tenant_id: str, key_id, target_agent_key: str,
                                       actor_agent_key: str | None):
        """The write authority must be a SERVICE-ACCOUNT key (the identity authority),
        NEVER an agent's own ingestion key (those are is_service_account=false). And an
        actor may not calibrate its own identity — reject a self-identity write."""
        if actor_agent_key and actor_agent_key == target_agent_key:
            raise HTTPException(403, "self-identity write refused: an actor cannot set its own calibration")
        if not key_id:
            raise HTTPException(403, "calibration:write requires a provisioned authority key")
        conn = pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT is_service_account FROM tenant_api_keys WHERE key_id = %s", (key_id,))
                row = cur.fetchone()
        finally:
            pool.putconn(conn)
        is_sa = (row[0] if not isinstance(row, dict) else row.get("is_service_account")) if row else False
        if not is_sa:
            raise HTTPException(403, "calibration:write is restricted to the service-account identity authority")

    def _write_calibration_audited(pool, tenant_id: str, agent_key: str, w: float,
                                   n_obs: int, method: str | None, key_id) -> str:
        """Write agent_calibration AND emit the audit gcrumb in ONE transaction — a
        reputation-affecting write can never persist without its audit trail (or vice
        versa). gcrumbs is REQUIRED (503 upstream if absent); the payload carries only
        public identifiers (the agent_key pubkey) + the weight — no tenant content."""
        conn = pool.getconn()
        try:
            with conn.transaction():                     # atomic: both INSERTs or neither
                with conn.cursor() as cur:
                    cur.execute("SELECT set_config('app.current_tenant', %s, false)", (tenant_id,))
                    cur.execute(
                        "INSERT INTO agent_calibration (tenant_id, agent_key, calibration_weight, "
                        "n_observations, method, as_of) VALUES (%s,%s,%s,%s,%s, now()) "
                        "ON CONFLICT (tenant_id, agent_key) DO UPDATE SET "
                        "calibration_weight=EXCLUDED.calibration_weight, "
                        "n_observations=EXCLUDED.n_observations, method=EXCLUDED.method, as_of=now()",
                        (tenant_id, agent_key, w, int(n_obs), method))
                payload = {
                    "agent_key": agent_key, "calibration_weight": w,
                    "n_observations": int(n_obs), "method": method or "",
                    "authority_key_id": key_id or "", "schema": "cgr.calibration.v1",
                }
                # same conn ⇒ the breadcrumb write joins this transaction (no self-commit)
                bc = gcrumbs.append_breadcrumb(tenant_id, "cgr:calibration:write", payload, conn=conn)
            return bc.get("breadcrumb_id")
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT set_config('app.current_tenant', '', false)")
            except Exception:
                pass
            pool.putconn(conn)

    class _CalibrationBody(BaseModel):
        calibration_weight: float
        n_observations: int = 0
        method: str | None = None

    @router.put("/calibration/{agent_key}")
    async def put_calibration(agent_key: str, body: _CalibrationBody, request: Request):
        require_scope(request, "calibration:write")
        pool = _calib_conn()
        ctx = getattr(request.state, "tenant", None)
        tenant_id = getattr(ctx, "tenant_id", None)
        key_id = getattr(ctx, "key_id", None)
        if not tenant_id:
            raise HTTPException(400, "tenant context required")
        w = float(body.calibration_weight)
        if not (0.0 <= w <= 1.0):
            raise HTTPException(400, "calibration_weight must be in [0, 1]")
        if not agent_key or len(agent_key) < 16:
            raise HTTPException(400, "invalid agent_key")
        if gcrumbs is None:
            raise HTTPException(503, "calibration:write requires the audit chain (gcrumbs) — refusing an unaudited write")
        actor = request.headers.get("X-Actor-Agent-Key")  # belt-and-suspenders self-guard
        _require_calibration_authority(pool, tenant_id, key_id, agent_key, actor)
        evidence_ref = _write_calibration_audited(pool, tenant_id, agent_key, w,
                                                  body.n_observations, body.method, key_id)
        return {"tenant_id": tenant_id, "agent_key": agent_key, "calibration_weight": w,
                "n_observations": body.n_observations, "evidence_ref": evidence_ref}

    @router.get("/issuer")
    async def get_issuer(request: Request):
        # Public by design — it's the public key a verifier fetches + pins. No scope.
        identity = _foundation()
        return {
            "issuer": ISSUER,
            "issuer_key_id": issuer_key_id(identity),
            "public_key": identity.public_key().hex(),
            "schema": CGR_ATTESTATION_SCHEMA,
        }

    @router.get("/attestations")
    async def list_attestations(request: Request, limit: int = 500, offset: int = 0):
        tenant_id = _tenant_id(request)
        require_scope(request, "cgr:read")
        _scoring_guard()
        _foundation()
        results = compute_scores(decision_trail, store_manager, tenant_id, limit=limit, offset=offset)
        atts = [_issue(tenant_id, r) for r in results]
        return {
            "attestations": atts,
            "count": len(atts),
            "issuer": ISSUER,
            "schema": CGR_ATTESTATION_SCHEMA,
            "as_of": results[0].as_of if results else _now_iso(),
        }

    @router.get("/attestation/{agent_handle:path}")
    async def get_attestation(agent_handle: str, request: Request, limit: int = 500, offset: int = 0):
        tenant_id = _tenant_id(request)
        require_scope(request, "cgr:read")
        _scoring_guard()
        _foundation()
        results = compute_scores(decision_trail, store_manager, tenant_id, limit=limit, offset=offset)
        match = next((r for r in results if r.agent_handle == agent_handle), None)
        if match is None:
            raise HTTPException(404, f"No CGR score for agent_handle {agent_handle!r}")
        # An `unproven` agent still gets a valid signed attestation (honest cold-start).
        return _issue(tenant_id, match)

    return router
