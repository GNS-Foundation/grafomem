"""Governed-decision, independent-verification, and CGR substrate-capture routes.

Kapwork surface:
  POST /v1/governed/decisions      Record a governed (judgment) decision.
  POST /v1/governed/verify-batch   Rules-engine batch; every decision tagged "rule".
  GET  /v1/gcrumbs/verify/key       Public Ed25519 key (auth-exempt).
  POST /v1/gcrumbs/verify           Stateless receipt verification.

CGR substrate capture (Ticket #1) — instrument the path so it accumulates what
the capability-grounded reputation layer needs, from the first invoice:
  * every governed decision carries the three irreversible CGR fields
    (invoice_ref, agent_handle, verifiability_tag) + a structured reason_code
    in its decision `parameters` (JSONB — no migration);
  POST /v1/governed/outcomes       Append-only intake of the ground-truth label
                                   (paid/default/…) that arrives weeks later.
  POST /v1/governed/outcomes/bulk  Same, list form (CSV day-one).
  GET  /v1/cgr/substrate/export    Joined decisions + outcomes for the offline
                                   CGR-v1 pass (shaped for cgr_substrate.py).

No DB migration: CGR decision fields ride `decision_records.parameters`;
outcomes ride a dedicated append-only GMP store ("cgr-outcomes"). The
signing/gcrumbs receipt logic is unchanged.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from aml.backends.interface import Capability, WriteOptions
from aml.cloud.execution_receipts import ExecutionReceiptService

# The CGR outcome-store read/join is owned by aml.cgr.substrate (single source of
# truth, shared with the scoring engine). The write path below reuses these.
from aml.cgr.substrate import (
    CGR_OUTCOMES_STORE, CGR_OUTCOME_SCHEMA, CGR_REVIEWS_STORE, CGR_REVIEW_SCHEMA,
    CGR_ROTATION_STORE, CGR_ROTATION_SCHEMA,
    _effective_at, _latest_for, _latest_review_for, _sort_key,
    _tenant_outcomes, _tenant_reviews, export_reviews, export_rotations, export_rows,
    load_substrate,
)

logger = logging.getLogger("grafomem.cloud.demo_routes")

# CGR substrate constants (store ids / schema markers imported from aml.cgr.substrate)
CGR_DECISION_SCHEMA = "cgr.decision.v1"
_OUTCOME_PREDICATE = "receivable_outcome"
_REVIEW_PREDICATE = "certification_review"
_VALID_OUTCOMES = {"paid", "default", "disputed", "late", "written_off"}


def _tenant_id(request: Request) -> str:
    ctx = getattr(request.state, "tenant", None)
    if ctx is None:
        raise HTTPException(401, "Authentication required")
    return ctx.tenant_id


def _tenant_encryption(request: Request):
    """Best-effort per-tenant at-rest encryptor (TenantKeyManager) from app state.

    Returns None when app state is absent (tests/dev with mock requests), so governed
    writes fall back to plaintext exactly as before — backward-compatible. In prod
    `app.state.encryption` is the TenantKeyManager, so the decision `query` (context)
    is encrypted at rest. Accessed defensively: mock requests have `.state` but no `.app`."""
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    return getattr(state, "encryption", None)


# ============================================================================
# Request models
# ============================================================================

class GovernedDecisionRequest(BaseModel):
    decision: str                                    # "certify" | "reject"
    reason: str = ""
    invoice_id: str | None = None
    context: dict = Field(default_factory=dict)      # the invoice fields
    model_id: str = "kapwork-verify-agent-v1"
    # CGR substrate (Ticket #1)
    agent_handle: str = "invoice-certifier@kapwork-receivables"
    verifiability_tag: str = "judgment"              # agent-posted judgment calls
    agent_tier: float | None = None                  # optional GEIANT TierGate snapshot
    # CGR identity binding (Ticket #5) — the acting agent's GEIANT Ed25519 pubkey
    # (64-hex), supplied by the emitter at decision time. Irreversible; NEVER
    # back-resolved from agent_handle. Absent ⇒ null ⇒ unbindable (unproven).
    agent_key: str | None = None


class VerifyBatchRequest(BaseModel):
    invoices: list[dict]                             # raw invoice objects
    policy: dict = Field(default_factory=dict)       # optional overrides of DEFAULT_POLICY
    model_id: str = "kapwork-verify-agent-v1"
    # CGR substrate (Ticket #1) — verify-batch decisions are always tag="rule"
    agent_handle: str = "invoice-rules-engine@kapwork-receivables"
    agent_tier: float | None = None
    agent_key: str | None = None                     # CGR identity binding (Ticket #5)


class OutcomeEvent(BaseModel):
    invoice_ref: str
    outcome: str                                     # paid|default|disputed|late|written_off
    outcome_date: str | None = None                  # ISO; default = now
    amount_recovered: float | None = None
    source: str = "manual"                           # funder_feed|kapwork_ledger|manual


class ReviewRecord(BaseModel):
    """A funder/analyst rating of a certification (Ticket #3). Enables the
    "verify the reviewer" bridge — calibrate a reviewer on verifiable outcomes,
    then trust their ratings on unverifiable calls. Many-per-invoice: dedup key is
    (invoice_ref, reviewer_handle)."""
    invoice_ref: str
    reviewer_handle: str
    rating: float                                    # [0, 1]
    agent_handle: str | None = None                  # who certified; back-filled at score time if omitted
    decision_id: str | None = None                   # precise referent (kept in metadata)
    review_date: str | None = None                   # ISO; default = now
    source: str = "manual"                           # funder_feed|analyst|manual


class RotationProofRequest(BaseModel):
    """A self-certifying key-rotation link (Ticket #7), EMITTER-supplied: the agent's
    OLD key signs {prev_key, new_key, seq, not_before}. Stored raw (append-only);
    the signature is verified at aggregation time, never trusted on write."""
    prev_key: str                                    # 64-hex — key being rotated out (the signer)
    new_key: str                                     # 64-hex — successor key
    seq: int = 1                                     # position in the chain
    not_before: str | None = None                    # ISO; default = now
    sig: str                                         # 128-hex Ed25519 signature by prev_key
    source: str = "agent"


# ============================================================================
# Decision record + sign (with CGR substrate fields in parameters)
# ============================================================================

def _record_and_sign(decision_trail, execution_receipts, signing_identity, *,
                     tenant_id, invoice_ref, context, decision, reason, model_id,
                     agent_handle, verifiability_tag, agent_tier, reason_code,
                     agent_key=None, encryption=None):
    """Record a governed decision as a signed decision_record + signed, chained
    execution_receipt, carrying the CGR substrate fields in `parameters`.
    Returns {decision_record, execution_receipt}."""
    if invoice_ref is None:
        logger.warning("CGR: governed decision recorded with no invoice_ref — will be unjoinable to outcome")

    query = json.dumps(context, sort_keys=True, default=str)
    raw_output = json.dumps({"decision": decision, "reason": reason}, sort_keys=True, default=str)

    rec = decision_trail.log(
        tenant_id=tenant_id, store_id="governed", query=query,
        model_id=model_id, raw_output=raw_output,
        parameters={
            "invoice_id": invoice_ref,               # existing
            "invoice_ref": invoice_ref,              # explicit CGR join key (alias, keep both)
            "decision": decision,                    # existing
            "reason_code": reason_code,              # structured code (rule) or None (judgment)
            "agent_handle": agent_handle,            # CGR: human-readable label (facet@territory)
            "agent_key": agent_key,                  # CGR: GEIANT identity pubkey — the binding subject (#5)
            "verifiability_tag": verifiability_tag,  # CGR: "rule" | "judgment"
            "agent_tier": agent_tier,                # CGR: optional TierGate snapshot (nullable)
            "cgr_schema": CGR_DECISION_SCHEMA,        # CGR: substrate version tag
        },
        signing_identity=signing_identity,
        # PII-at-rest (#Mauricio gate B): encrypt the decision `query` (= the governed
        # `context`, which may carry invoice/party PII). Same class as propose_action.
        # CGR is unaffected — it reads `parameters` (never encrypted), not `query`.
        encryption=encryption,
    )
    workflow_id = f"governed:{invoice_ref or tenant_id}"
    try:
        step_number = len(execution_receipts.get_receipts(workflow_id))
    except Exception:
        step_number = 0
    receipt = execution_receipts.issue_receipt(
        tenant_id=tenant_id, step_id=uuid.uuid4().hex, workflow_id=workflow_id,
        step_number=step_number, input_text=query, retrieved_contents=[],
        governance_logs=[{"decision": decision, "reason": reason}],
        model_id=model_id, raw_output=raw_output, decision_id=rec.decision_id,
    )
    return {
        "decision_record": {
            "decision_id": rec.decision_id, "tenant_id": rec.tenant_id,
            "invoice_id": invoice_ref, "decision": decision, "reason": reason,
            "reason_code": reason_code, "agent_handle": agent_handle,
            "verifiability_tag": verifiability_tag, "agent_tier": agent_tier,
            "model_id": rec.model_id, "raw_output": rec.raw_output,
            "created_at": rec.created_at.isoformat(),
            "signature": base64.b64encode(rec.signature).decode() if rec.signature else None,
            "public_key": base64.b64encode(rec.public_key).decode() if rec.public_key else None,
        },
        "execution_receipt": ExecutionReceiptService.receipt_to_dict(receipt),
    }


# ============================================================================
# CGR outcomes store — append-only, tenant-scoped
# ============================================================================

def _parse_dt(s: str | None) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _outcome_metadata(invoice_ref: str, outcome: str, amount_recovered, source: str) -> dict:
    # Fact-shaped record carried in content + metadata (the GMP write API is
    # content-based): predicate="receivable_outcome", subject=invoice_ref, object=outcome.
    return {
        "predicate": _OUTCOME_PREDICATE, "subject": invoice_ref, "object": outcome,
        "amount_recovered": amount_recovered, "source": source,
        "cgr_schema": CGR_OUTCOME_SCHEMA,
    }


def _record_outcome(backend, *, tenant_id, invoice_ref, outcome, outcome_date,
                    amount_recovered, source) -> dict:
    """Append-only write of an outcome. Idempotent on an identical re-post;
    supersedes the prior when it differs and the backend supports it."""
    existing = _tenant_outcomes(backend, tenant_id)
    current = _latest_for(existing, invoice_ref)

    # (3) Idempotent: an identical (outcome, amount_recovered, source) re-post is a no-op.
    if current is not None:
        cm = current.metadata or {}
        if (cm.get("object") == outcome and cm.get("amount_recovered") == amount_recovered
                and cm.get("source") == source):
            return {"invoice_ref": invoice_ref, "outcome": outcome,
                    "recorded_at": _effective_at(current).isoformat(),
                    "superseded_prior": False, "idempotent": True}

    vf = _parse_dt(outcome_date)
    meta = _outcome_metadata(invoice_ref, outcome, amount_recovered, source)
    opts = WriteOptions(valid_from=vf, tenant_id=tenant_id, metadata=meta)
    content = f"{_OUTCOME_PREDICATE} | {invoice_ref} | {outcome}"

    superseded = current is not None
    if current is not None:
        # (2) supersede is OPTIONAL — check capability, fall back to append.
        caps = getattr(backend, "capabilities", lambda: set())()
        did = False
        if Capability.SUPERSESSION_CHAIN in caps:
            try:
                backend.supersede(current.ref, content, meta, opts)   # PostgresGMPBackend sig
                did = True
            except TypeError:
                try:
                    backend.supersede(current.ref, content, opts)     # Protocol 3-arg sig
                    did = True
                except Exception:
                    did = False
            except Exception:
                did = False
        if not did:
            backend.write(content, opts)   # append-only fallback; latest valid_from wins
    else:
        backend.write(content, opts)

    return {"invoice_ref": invoice_ref, "outcome": outcome, "recorded_at": vf.isoformat(),
            "superseded_prior": superseded, "idempotent": False}


# ============================================================================
# CGR reviews store — append-only, tenant-scoped, keyed by (invoice_ref, reviewer)
# ============================================================================

def _review_metadata(invoice_ref, reviewer_handle, rating, agent_handle, decision_id, source) -> dict:
    # Fact-shaped: predicate="certification_review", subject=invoice_ref (join key,
    # consistent with the outcome store), object=rating. reviewer_handle/decision_id
    # in metadata (decision_id kept for precise attribution; subject stays invoice_ref).
    return {
        "predicate": _REVIEW_PREDICATE, "subject": invoice_ref, "object": rating,
        "reviewer_handle": reviewer_handle, "agent_handle": agent_handle,
        "decision_id": decision_id, "source": source,
        "cgr_schema": CGR_REVIEW_SCHEMA,
    }


def _record_review(backend, *, tenant_id, invoice_ref, reviewer_handle, rating,
                   agent_handle, decision_id, review_date, source) -> dict:
    """Append-only write of a review. Dedup/revision key is (invoice_ref,
    reviewer_handle): a reviewer re-rating supersedes their OWN prior; a different
    reviewer is a distinct record. Idempotent on an identical re-post."""
    existing = _tenant_reviews(backend, tenant_id)
    current = _latest_review_for(existing, invoice_ref, reviewer_handle)

    # Idempotent: identical (rating, agent_handle, source) from the same reviewer is a no-op.
    if current is not None:
        cm = current.metadata or {}
        if (cm.get("object") == rating and cm.get("agent_handle") == agent_handle
                and cm.get("source") == source):
            return {"invoice_ref": invoice_ref, "reviewer_handle": reviewer_handle,
                    "rating": rating, "recorded_at": _effective_at(current).isoformat(),
                    "superseded_prior": False, "idempotent": True}

    vf = _parse_dt(review_date)
    meta = _review_metadata(invoice_ref, reviewer_handle, rating, agent_handle, decision_id, source)
    opts = WriteOptions(valid_from=vf, tenant_id=tenant_id, metadata=meta)
    content = f"{_REVIEW_PREDICATE} | {invoice_ref} | {reviewer_handle} | {rating}"

    superseded = current is not None
    if current is not None:
        # supersede is OPTIONAL — check capability, fall back to append (latest wins).
        caps = getattr(backend, "capabilities", lambda: set())()
        did = False
        if Capability.SUPERSESSION_CHAIN in caps:
            try:
                backend.supersede(current.ref, content, meta, opts)   # PostgresGMPBackend sig
                did = True
            except TypeError:
                try:
                    backend.supersede(current.ref, content, opts)     # Protocol 3-arg sig
                    did = True
                except Exception:
                    did = False
            except Exception:
                did = False
        if not did:
            backend.write(content, opts)
    else:
        backend.write(content, opts)

    return {"invoice_ref": invoice_ref, "reviewer_handle": reviewer_handle,
            "rating": rating, "recorded_at": vf.isoformat(),
            "superseded_prior": superseded, "idempotent": False}


# ============================================================================
# CGR identity store (Ticket #7) — append-only key-rotation proofs, keyed by new_key
# ============================================================================

def _rotation_metadata(p: RotationProofRequest) -> dict:
    # Fact-shaped: subject == new_key (the successor being claimed). The whole signed
    # link rides in metadata; verification is deferred to aggregation time.
    return {
        "predicate": "key_rotation", "subject": p.new_key, "object": p.prev_key,
        "prev_key": p.prev_key, "seq": p.seq, "not_before": p.not_before,
        "sig": p.sig, "source": p.source,
        "cgr_schema": CGR_ROTATION_SCHEMA,
    }


def _record_rotation(backend, *, tenant_id, p: RotationProofRequest) -> dict:
    """Append-only write of a rotation proof. RAW — no signature check here (a
    tampered row must not confer continuity; the engine verifies every link before
    folding keys). Append, latest valid_from wins if a (prev,new) is re-posted."""
    vf = _parse_dt(p.not_before)
    meta = _rotation_metadata(p)
    opts = WriteOptions(valid_from=vf, tenant_id=tenant_id, metadata=meta)
    content = f"key_rotation | {p.prev_key} | {p.new_key} | seq={p.seq}"
    backend.write(content, opts)
    return {"prev_key": p.prev_key, "new_key": p.new_key, "seq": p.seq,
            "not_before": vf.isoformat(), "recorded": True}


# ============================================================================
# Routers
# ============================================================================

def create_governed_router(decision_trail, execution_receipts, signing_identity,
                           store_manager=None) -> APIRouter:
    router = APIRouter(tags=["Governed Decisions"])

    def _guard():
        if execution_receipts is None or decision_trail is None:
            raise HTTPException(503, "governed-decision services not available")

    def _outcomes_backend():
        if store_manager is None:
            raise HTTPException(503, "outcomes store not available")
        return store_manager.get_or_create_named(CGR_OUTCOMES_STORE).backend

    def _reviews_backend():
        if store_manager is None:
            raise HTTPException(503, "reviews store not available")
        return store_manager.get_or_create_named(CGR_REVIEWS_STORE).backend

    def _rotations_backend():
        if store_manager is None:
            raise HTTPException(503, "identity store not available")
        return store_manager.get_or_create_named(CGR_ROTATION_STORE).backend

    @router.post("/v1/governed/decisions")
    async def governed_decision(req: GovernedDecisionRequest, request: Request):
        tenant_id = _tenant_id(request)
        _guard()
        return _record_and_sign(
            decision_trail, execution_receipts, signing_identity,
            tenant_id=tenant_id, invoice_ref=req.invoice_id, context=req.context,
            decision=req.decision, reason=req.reason, model_id=req.model_id,
            agent_handle=req.agent_handle, verifiability_tag=req.verifiability_tag,
            agent_tier=req.agent_tier, reason_code=None,  # judgment: no rule reason_code
            agent_key=req.agent_key,
            encryption=_tenant_encryption(request),
        )

    @router.post("/v1/governed/verify-batch")
    async def verify_batch(req: VerifyBatchRequest, request: Request):
        """Ingest a batch of invoices, run the configurable rules engine
        SERVER-SIDE, and record each result as a signed governed decision
        (verifiability_tag="rule")."""
        tenant_id = _tenant_id(request)
        _guard()
        from aml.cloud.verification import evaluate_invoice, resolve_policy

        pol = resolve_policy(req.policy)
        id_field, vendor_field, debtor_field = pol["invoice_id_field"], pol["vendor_field"], pol["debtor_field"]
        certified: set = set()
        results = []
        for raw in req.invoices:
            inv = {k: v for k, v in raw.items() if not str(k).startswith("_")}
            decision, reason_code, reason = evaluate_invoice(inv, req.policy, certified)
            inv_id = inv.get(id_field)
            packet = _record_and_sign(
                decision_trail, execution_receipts, signing_identity,
                tenant_id=tenant_id, invoice_ref=inv_id, context=inv,
                decision=decision, reason=reason, model_id=req.model_id,
                agent_handle=req.agent_handle, verifiability_tag="rule",
                agent_tier=req.agent_tier, reason_code=reason_code,
                agent_key=req.agent_key,
                encryption=_tenant_encryption(request),
            )
            if decision == "certify":
                certified.add(inv_id)
            results.append({
                "invoice_id": inv_id,
                "vendor": inv.get(vendor_field), "debtor": inv.get(debtor_field),
                "decision": decision, "reason": reason, "reason_code": reason_code,
                "decision_record": packet["decision_record"],
                "execution_receipt": packet["execution_receipt"],
            })

        n_cert = sum(1 for r in results if r["decision"] == "certify")
        return {
            "summary": {"total": len(results), "certified": n_cert, "rejected": len(results) - n_cert},
            "policy": pol,
            "results": results,
        }

    @router.post("/v1/governed/outcomes")
    async def post_outcome(ev: OutcomeEvent, request: Request):
        tenant_id = _tenant_id(request)
        if ev.outcome not in _VALID_OUTCOMES:
            raise HTTPException(400, f"outcome must be one of {sorted(_VALID_OUTCOMES)}")
        return _record_outcome(
            _outcomes_backend(), tenant_id=tenant_id, invoice_ref=ev.invoice_ref,
            outcome=ev.outcome, outcome_date=ev.outcome_date,
            amount_recovered=ev.amount_recovered, source=ev.source,
        )

    @router.post("/v1/governed/outcomes/bulk")
    async def post_outcomes_bulk(events: list[OutcomeEvent], request: Request):
        tenant_id = _tenant_id(request)
        backend = _outcomes_backend()
        recorded = []
        for ev in events:
            if ev.outcome not in _VALID_OUTCOMES:
                raise HTTPException(400, f"outcome must be one of {sorted(_VALID_OUTCOMES)}")
            recorded.append(_record_outcome(
                backend, tenant_id=tenant_id, invoice_ref=ev.invoice_ref,
                outcome=ev.outcome, outcome_date=ev.outcome_date,
                amount_recovered=ev.amount_recovered, source=ev.source))
        return {"count": len(recorded), "recorded": recorded}

    def _validate_rating(rating: float):
        if not (0.0 <= rating <= 1.0):
            raise HTTPException(400, "rating must be in [0, 1]")

    @router.post("/v1/governed/reviews")
    async def post_review(rv: ReviewRecord, request: Request):
        tenant_id = _tenant_id(request)
        _validate_rating(rv.rating)
        return _record_review(
            _reviews_backend(), tenant_id=tenant_id, invoice_ref=rv.invoice_ref,
            reviewer_handle=rv.reviewer_handle, rating=rv.rating,
            agent_handle=rv.agent_handle, decision_id=rv.decision_id,
            review_date=rv.review_date, source=rv.source,
        )

    @router.post("/v1/cgr/rotation")
    async def post_rotation(p: RotationProofRequest, request: Request):
        """Capture an emitter-supplied key-rotation proof (Ticket #7). Stored raw,
        append-only; the signature is verified at scoring time before keyA and keyB
        are folded into one identity."""
        tenant_id = _tenant_id(request)
        return _record_rotation(_rotations_backend(), tenant_id=tenant_id, p=p)

    @router.get("/v1/cgr/rotations")
    async def get_rotations(request: Request, anchor: str | None = None, current: str | None = None):
        """Serve the captured, self-certifying rotation proofs read-only (Ticket
        #10a) so a consumer (geiant) can INDEPENDENTLY verify anchor→current instead
        of trusting grafomem's re-issue. Each proof is served raw (byte-parity with
        what prev_key signed); the reader re-checks every signature — the server is
        untrusted transport, never an authority on continuity.

        Optional ?anchor=<hex> / ?current=<hex> filter to one identity's chain,
        resolved server-side via resolve_identities (a convenience; the reader still
        re-verifies). Read-only, no write path touched.

        Auth: tenant-scoped (parity with the reviews/substrate reads) — a valid
        tenant context is required. Because the proofs are self-certifying, this
        route COULD be made anonymous by adding "/v1/cgr/rotations" to auth.py's
        _SKIP_AUTH_PATHS (as /v1/cgr/issuer is); left tenant-scoped by default until
        a concrete anonymous-verifier need lands."""
        tenant_id = _tenant_id(request)
        if store_manager is None:
            raise HTTPException(503, "identity store not available")
        proofs = export_rotations(store_manager, tenant_id)
        if not (anchor or current):
            return {"rotations": proofs}

        # Resolve chains to filter by identity. Verification here is only for the
        # filter; the consumer re-verifies the raw proofs it receives.
        from aml.cgr.engine import _rotation_verifier
        from aml.cgr.identity import RotationProof, resolve_identities
        rp = []
        for p in proofs:
            try:
                rp.append(RotationProof(
                    prev_key=p["prev_key"], new_key=p["new_key"],
                    seq=int(p["seq"]) if p["seq"] is not None else 0,
                    not_before=p["not_before"] or "", sig=p["sig"]))
            except (TypeError, ValueError):
                continue
        anchor_of, _current_of, _frozen = resolve_identities(rp, verify=_rotation_verifier)
        target = anchor if anchor else anchor_of.get(current, current)
        filtered = [p for p in proofs if anchor_of.get(p["new_key"]) == target]
        return {"rotations": filtered}

    @router.post("/v1/governed/reviews/bulk")
    async def post_reviews_bulk(reviews: list[ReviewRecord], request: Request):
        tenant_id = _tenant_id(request)
        backend = _reviews_backend()
        recorded = []
        for rv in reviews:
            _validate_rating(rv.rating)
            recorded.append(_record_review(
                backend, tenant_id=tenant_id, invoice_ref=rv.invoice_ref,
                reviewer_handle=rv.reviewer_handle, rating=rv.rating,
                agent_handle=rv.agent_handle, decision_id=rv.decision_id,
                review_date=rv.review_date, source=rv.source))
        return {"count": len(recorded), "recorded": recorded}

    return router


def create_cgr_router(decision_trail, store_manager) -> APIRouter:
    """The read/join path so the validated cgr_substrate.py can run on real data."""
    from aml.server.scopes import require_scope

    router = APIRouter(prefix="/v1/cgr", tags=["CGR Substrate"])

    @router.get("/substrate/export")
    async def export_substrate(request: Request, limit: int = 500, offset: int = 0):
        tenant_id = _tenant_id(request)
        require_scope(request, "decisions:read")
        if decision_trail is None or store_manager is None:
            raise HTTPException(503, "CGR substrate services not available")

        # The join now lives in aml.cgr.substrate.load_substrate (single source of
        # truth, shared with the scoring engine). export_rows() reproduces the
        # historical 10-key decisions[] shape byte-for-byte; reviews[] is additive
        # (Ticket #3) — new top-level key, decisions[] + count unchanged.
        rows = load_substrate(decision_trail, store_manager, tenant_id, limit=limit, offset=offset)
        serialized = export_rows(rows)
        return {
            "decisions": serialized,
            "count": len(serialized),
            "reviews": export_reviews(store_manager, tenant_id),
        }

    return router


# ============================================================================
# GET /v1/gcrumbs/verify/key  +  POST /v1/gcrumbs/verify   (independent verifier)
# ============================================================================

class VerifyRequest(BaseModel):
    receipts: list[dict]
    public_key_b64: str | None = None   # if omitted, uses the key embedded in each receipt


def create_verify_router(signing_identity) -> APIRouter:
    router = APIRouter(prefix="/v1/gcrumbs", tags=["Independent Verification"])

    @router.get("/verify/key")
    def verify_key():
        if signing_identity is None:
            raise HTTPException(status_code=503, detail="no signing identity configured — receipts are unsigned")
        pub = signing_identity.public_key()
        return {
            "algorithm": "ed25519",
            "public_key_hex": pub.hex(),
            "public_key_b64": base64.b64encode(pub).decode(),
        }

    @router.post("/verify")
    def verify(req: VerifyRequest):
        if not req.receipts:
            return {"valid": False, "count": 0, "reason": "no receipts supplied", "results": []}
        results = []
        all_valid = True
        for i, r in enumerate(req.receipts):
            key = req.public_key_b64 or r.get("public_key_b64")
            valid, reason = ExecutionReceiptService.verify_receipt_dict(r, public_key_b64=key)
            results.append({
                "index": i,
                "receipt_id": r.get("receipt_id"),
                "valid": valid,
                "reason": reason,
            })
            all_valid = all_valid and valid
        return {"valid": all_valid, "count": len(results), "results": results}

    return router
