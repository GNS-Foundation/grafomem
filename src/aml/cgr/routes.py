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

import functools
from dataclasses import asdict

import anyio
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel


class CalibrationBody(BaseModel):
    """Body for PUT /v1/cgr/calibration/{agent_key} (module-level so FastAPI resolves it
    as the request body, not a query param)."""
    calibration_weight: float
    n_observations: int = 0
    method: str | None = None

from datetime import datetime, timezone

from aml.cgr import attestation as _attestation  # referenced LIVE so the emission-bump flip
from aml.cgr.attestation import (                 # (attestation.CGR_ATTESTATION_SCHEMA) propagates
    build_attestation,                            # here without a second edit — see get_issuer/list_attestations
)
from aml.cgr.engine import compute_scores, to_tiergate
from aml.cgr.gate import calibration_tenant_tx
from aml.cgr.identity import did_key
from aml.cgr.issuance import ISSUER, issuer_key_id, make_signer
from aml.cgr.scoring import DIMENSION_RECEIVABLES, _now_iso
from aml.cgr.substrate import load_substrate

# Ticket 2 — external READ surface.
READ_SURFACE_VERSION = "cgr-read/1"
# Staleness threshold for the freshness envelope. Served config, NOT signed — `stale` is an
# advisory hint a policy engine may override; the signed `last_resolved_at` is the fact.
READ_STALE_AFTER_DAYS = 30
_VERIFY_RECIPE_URL = "https://docs.grafomem.com/cgr/verify/"  # canonical trailing-slash (avoids a 308 redirect hop)
_VERIFIER_LIB = "@gns-foundation/cgr-verify"


def _read_freshness(last_resolved_at: str | None, *, now: datetime | None = None) -> dict:
    """Freshness block for the read envelope. `stale` is advisory (threshold is served
    config); the authoritative, signed fact is `last_resolved_at` in the attestation body.
    No resolved evidence yet ⇒ stale by definition."""
    now = now or datetime.now(timezone.utc)
    out = {"as_of": now.isoformat(), "last_resolved_at": last_resolved_at,
           "age_ms": None, "stale": True}
    if last_resolved_at:
        try:
            dt = datetime.fromisoformat(last_resolved_at.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_ms = (now - dt).total_seconds() * 1000.0
            out["age_ms"] = age_ms
            out["stale"] = age_ms > READ_STALE_AFTER_DAYS * 86400_000
        except Exception:
            pass  # unparseable timestamp ⇒ leave stale=True (fail-safe)
    return out


def _read_continuity(subject_key: str | None, subject_did: str | None) -> dict:
    """ADVISORY continuity status for the read envelope — the consumer MUST re-verify
    independently (fetch /v1/cgr/rotations + run the verifier). verified = no rotation
    (anchor == current); asserted = a rotation occurred, server asserts continuity but
    the consumer must re-walk the chain; unverified = no bound key."""
    if not subject_key:
        return {"status": "unverified", "advisory": True,
                "note": "no bound subject_key — unbindable"}
    if subject_did and did_key(subject_key) == subject_did:
        return {"status": "verified", "advisory": True,
                "note": "no rotation (anchor == current); re-verify via /v1/cgr/rotations"}
    return {"status": "asserted", "advisory": True,
            "note": "rotation present — re-walk the chain via /v1/cgr/rotations + the verifier"}


def build_read_envelope(match, requested_domain, domain_n_resolved, *, signer,
                        issuer_key_id_hex: str, issuer_pubkey_hex: str, anchor=None,
                        now: datetime | None = None) -> dict:
    """Pure envelope assembly (no DB/routing) — unit-testable. Mints the fresh v3
    attestation with the SIGNED scope fields (requested_domain, domain_n_resolved),
    then wraps it so `score` is INSEPARABLE from its two evidence masses + freshness:
    the pooled `evidence_mass`/`n_resolved` back the score, `domain_n_resolved` backs the
    domain match, and the authoritative copies of all of these live signed inside `att`.
    `anchor(tiergate, att) -> evidence_ref` is optional (the gcrumbs pointer)."""
    tg = to_tiergate(match)
    tg["requested_domain"] = requested_domain
    tg["domain_n_resolved"] = domain_n_resolved
    # recorded_at (v4 issue time) is threaded from the request `now` for determinism; it is only
    # written into the signed body once the schema is v4 (dormant under v3 — see build_attestation).
    att = build_attestation(tg, signer=signer, issuer_key_id=issuer_key_id_hex, evidence_ref=None,
                            recorded_at=(now.isoformat() if now is not None else None))
    if anchor is not None:
        att["evidence_ref"] = anchor(tg, att)
    return {
        "surface_version": READ_SURFACE_VERSION,
        "result": "attestation",
        "attestation": att,
        "score": match.cgr_score,
        "evidence_mass": match.confidence,        # pooled n = α+β — backs the score
        "n_resolved": match.n_resolved,           # pooled resolved count — backs the score
        "scoring_scope": "pooled",                # NOT a per-domain score
        "requested_domain": requested_domain,     # convenience copy (authoritative copy signed in att)
        "domain_n_resolved": domain_n_resolved,   # backs the domain MATCH (convenience copy; signed in att)
        # v4 convenience echoes — present only once the schema flips (authoritative copies signed in att);
        # the spread keeps the v3 envelope shape byte-for-byte unchanged while v3 is live.
        **({"recorded_at": att["recorded_at"], "verifiability_tag": att["verifiability_tag"]}
           if "recorded_at" in att else {}),
        "freshness": _read_freshness(match.last_resolved_at, now=now),
        "issuer": {"issuer": ISSUER, "issuer_key_id": issuer_key_id_hex,
                   "schema": att["schema"]},   # the ACTUAL minted schema — single-constant flip
        "continuity": _read_continuity(match.subject_key, match.subject_did),
        "verify": {"recipe_url": _VERIFY_RECIPE_URL, "lib": _VERIFIER_LIB,
                   "issuer_pubkey": issuer_pubkey_hex},
    }


# ── Shared read-core (Phase 2) ────────────────────────────────────────────────
# The SAME logic behind GET /v1/cgr/read/attestation, factored out so the REST route
# AND the remote MCP tool call it — the signed envelope is then identical by
# construction, not "kept in sync". fastapi-free (returns dicts / raises ValueError),
# so a sidecar could host it unchanged. NO per-read anchor (uniform policy, Phase 2 Q1).

def read_no_evidence(reason: str) -> dict:
    """The explicit no-evidence result — never a default score, never 0.5."""
    return {"surface_version": READ_SURFACE_VERSION, "result": "no_evidence",
            "reason": reason, "score": None, "evidence_mass": None}


def resolve_subject(subject: str, key: str, did: str, handle: str):
    """(want_key, want_did, want_handle) from an explicit key/did/handle or the
    ?subject= heuristic (64-hex ⇒ key, did:key: ⇒ did, else handle). ValueError if none."""
    want_key, want_did, want_handle = (key or None), (did or None), (handle or None)
    if subject and not (want_key or want_did or want_handle):
        s = subject.strip()
        if s.startswith("did:key:"):
            want_did = s
        elif len(s) == 64 and all(c in "0123456789abcdefABCDEF" for c in s):
            want_key = s.lower()
        else:
            want_handle = s
    if not (want_key or want_did or want_handle):
        raise ValueError("provide subject via subject (key|did|handle) or key/did/handle")
    return want_key, want_did, want_handle


def build_read_result(decision_trail, store_manager, foundation_identity, tenant_id: str, *,
                      subject: str = "", key: str = "", did: str = "", handle: str = "",
                      domain: str = "", limit: int = 500, offset: int = 0) -> dict:
    """Envelope for (subject, domain) or an explicit no_evidence dict. Caller guarantees
    foundation_identity is not None (503 upstream otherwise). No anchor (Phase 2 Q1)."""
    want_key, want_did, want_handle = resolve_subject(subject, key, did, handle)
    results = compute_scores(decision_trail, store_manager, tenant_id, limit=limit, offset=offset)

    def _match(r):
        return ((want_key and r.subject_key == want_key)
                or (want_did and r.subject_did == want_did)
                or (want_handle and r.agent_handle == want_handle))

    match = next((r for r in results if _match(r)), None)
    if match is None:
        return read_no_evidence("no CGR score for the requested subject on this tenant")

    requested_domain = domain or None
    domain_n_resolved = None
    if requested_domain is not None:
        rows = load_substrate(decision_trail, store_manager, tenant_id, limit=limit, offset=offset)
        subj_rows = [rw for rw in rows if (
            (match.subject_key and rw.agent_key == match.subject_key)
            or (not match.subject_key and rw.agent_handle == match.agent_handle))]
        dom_rows = [rw for rw in subj_rows if rw.cgr_domain == requested_domain]
        if not dom_rows:
            return read_no_evidence(
                f"subject has no captured evidence in domain {requested_domain!r}")
        domain_n_resolved = sum(
            1 for rw in dom_rows
            if rw.decision == "certify" and rw.verifiability_tag == "judgment"
            and rw.outcome in ("paid", "default"))

    return build_read_envelope(
        match, requested_domain, domain_n_resolved,
        signer=make_signer(foundation_identity),
        issuer_key_id_hex=issuer_key_id(foundation_identity),
        issuer_pubkey_hex=foundation_identity.public_key().hex(),
        anchor=None,   # uniform no-per-read-anchor (Phase 2 Q1)
    )


def list_subject_domains(decision_trail, store_manager, tenant_id: str, *,
                         subject: str = "", key: str = "", did: str = "", handle: str = "",
                         limit: int = 500, offset: int = 0) -> dict:
    """Distinct captured cgr_domain values for a subject (read-only, no scores)."""
    want_key, want_did, want_handle = resolve_subject(subject, key, did, handle)
    rows = load_substrate(decision_trail, store_manager, tenant_id, limit=limit, offset=offset)
    subj_rows = [rw for rw in rows if (
        (want_key and rw.agent_key == want_key)
        or (want_handle and rw.agent_handle == want_handle)
        or (want_did and rw.agent_key and did_key(rw.agent_key) == want_did))]
    domains = sorted({rw.cgr_domain for rw in subj_rows if rw.cgr_domain})
    return {"surface_version": READ_SURFACE_VERSION, "result": "domains",
            "subject": subject or key or did or handle, "domains": domains}


def _tenant_id(request: Request) -> str:
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(401, "Authentication required")
    return ctx.tenant_id


def _write_calibration_audited(pool, gcrumbs, tenant_id: str, agent_key: str, w: float,
                               n_obs: int, method: str | None, key_id) -> str:
    """Write agent_calibration AND emit the audit gcrumb in ONE transaction-local scope
    — a reputation-affecting write can never persist without its audit trail (or vice
    versa). `agent_calibration` is RLS FORCE + WITH CHECK, so the single
    transaction-local tenant GUC (= tenant_id) both authorizes the upsert (WITH CHECK)
    and scopes the breadcrumb write, which joins this same connection/transaction
    (`gcrumbs_breadcrumbs` is same-tenant, RLS-off, explicit `WHERE tenant_id`). gcrumbs
    is REQUIRED (503 upstream if absent); the payload carries only public identifiers
    (the agent_key pubkey) + the weight — no tenant content.

    Module-level (not nested in the router factory) so the real write path is unit
    testable under a restricted RLS role. Transaction-local GUC via
    ``calibration_tenant_tx`` — the fragile session-scoped setter + finally-reset was
    removed here to match the world-model pattern (grafomem PR #46)."""
    with calibration_tenant_tx(pool, tenant_id) as conn:     # atomic: both INSERTs or neither
        with conn.cursor() as cur:
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
        # same conn ⇒ the breadcrumb write joins this transaction (no self-commit).
        # bc is a plain dict materialized inside the scope — the return does NOT read
        # the DB after commit, so there is no post-transaction GUC-dependent read.
        bc = gcrumbs.append_breadcrumb(tenant_id, "cgr:calibration:write", payload, conn=conn)
        return bc.get("breadcrumb_id")


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
        """Score result -> to_tiergate -> Foundation-signed attestation.

        Phase 2 (uniform no-per-read-anchor): reads do NOT write to the gcrumbs chain.
        The attestation is deterministically re-minted over evidence already anchored at
        issuance and is Foundation-signed + offline-verifiable, so nothing is lost by not
        anchoring the read; `evidence_ref` is therefore None. (A read is not a state change
        — an optional out-of-band access-audit log may be added later, non-gating.)"""
        identity = _foundation()
        signer = make_signer(identity)
        kid = issuer_key_id(identity)
        tiergate = to_tiergate(result)
        att = build_attestation(tiergate, signer=signer, issuer_key_id=kid, evidence_ref=None)
        return att

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

    @router.put("/calibration/{agent_key}")
    async def put_calibration(agent_key: str, body: CalibrationBody, request: Request):
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
        evidence_ref = _write_calibration_audited(pool, gcrumbs, tenant_id, agent_key, w,
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
            "schema": _attestation.CGR_ATTESTATION_SCHEMA,
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
            "schema": _attestation.CGR_ATTESTATION_SCHEMA,
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

    # ── Ticket 2: the external READ surface (AUTHENTICATED ONLY) ───────────────
    # Public/unauthenticated serving of dogfood (real-work) attestations is a HARD STOP
    # until the public-safe boundary spec is signed off — this endpoint requires cgr:read
    # + tenant exactly like the rest. Honest-scope by construction: score is only ever
    # returned INSIDE an envelope that also carries evidence mass + freshness; a bare
    # score is structurally unobtainable. Unknown subject / domain ⇒ explicit no_evidence.
    @router.get("/read/attestation")
    async def read_attestation(request: Request, subject: str = "", domain: str = "",
                               key: str = "", did: str = "", handle: str = "",
                               limit: int = 500, offset: int = 0):
        tenant_id = _tenant_id(request)
        require_scope(request, "cgr:read")          # authenticated only — public path is gated
        _scoring_guard()
        identity = _foundation()
        # Shared read-core, offloaded to a thread so the blocking sync-psycopg scan does not
        # stall the event loop (Phase 2 Q2A). This is the SAME core the remote MCP tool calls,
        # so the signed envelope is identical by construction. No per-read anchor (Phase 2 Q1).
        try:
            return await anyio.to_thread.run_sync(
                functools.partial(
                    build_read_result, decision_trail, store_manager, identity, tenant_id,
                    subject=subject, key=key, did=did, handle=handle, domain=domain,
                    limit=limit, offset=offset))
        except ValueError as e:
            raise HTTPException(400, str(e))

    return router
