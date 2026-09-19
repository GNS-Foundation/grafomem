"""cgr.disposition.v1 runtime — POST /v1/dispositions (attest) + GET /v1/dispositions/{id} (verify).

Implements the accepted cgr.cosign.v1 spec (docs/cgr/cgr-cosign-v1-spec.md, decisions 0009/0010/0011)
as the cgr.disposition.v1 record class, per the ratified profile registry
(docs/cgr/cosign-profile-registry.json) and grafomem-internal 2026-09-19-cgr-disposition-runtime.md.

Model (spec §3.2, operator decision 2): the runtime is the capturing **issuer** — it produces the
system **counter-signature** with its pinned signing identity and pins that key id as the sole trusted
issuer. So POST accepts the **approver-signed inner only** (content_body + approval_assertion +
approver_signature); a submitted record carrying a system_signature/system_metadata → 422. The runtime
verifies the approver layer, checks the approver is **enrolled** for the tenant (empty registry →
reject), counter-signs, and appends to the ledger-class `cosign_dispositions`. Assurance tier (gap 3a,
cosign §10) is read from the **enrolment** (never self-claimed) and **surfaced, never gated**.

Verification reuses the vendored reference verifier `aml.cgr.cosign_verify` (mirrors
packages/grafomem-cgr; the shared conformance corpus conformance/cgr-disposition-v1 keeps them aligned).

Storage note: `cosign_dispositions` is ledger-class (migration 016) — the runtime role is SELECT-only,
so the INSERT runs via a ledger-role writer. `ledger_pool` is that writer; on single-role deployments
(no grafomem_rt) it falls back to the main pool. Split-role prod MUST supply a ledger-role connection
to the main DB (see the design report — the one deployment config this route needs).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from aml.server.scopes import require_scope
from aml.cgr import cosign_verify as cv

logger = logging.getLogger("grafomem.cloud.dispositions")

_ENVELOPE_KEYS = ("system_signature", "evidence_ref")

# The ratified cgr.cosign.v1 profile registry, EMBEDDED so it ships with the package — a filesystem
# path to docs/cgr/ is not reliable in the deployed layout (docs/ is not packaged; __file__ may resolve
# under site-packages). This MIRRORS the normative docs/cgr/cosign-profile-registry.json; the test
# test_disposition_registry_matches_normative asserts they stay aligned (like the vendored verifier).
_PROFILE_REGISTRY = {
    "profiles": {
        "cgr.disposition.v1": {"approval_mode": "bound", "approver_signature": "REQUIRED"},
    }
}


class DispositionIn(BaseModel):
    """The approver-signed inner (Layer 1+2+3a). No system layer — the runtime counter-signs."""
    schema_: str = Field(..., alias="schema")
    profile: str
    approval_mode: str
    content_body: dict
    approval_assertion: dict
    approver_signature: str
    # A submitted system layer is rejected (decision 2); accepted here only to 422 explicitly.
    system_signature: str | None = None
    system_metadata: dict | None = None

    model_config = {"populate_by_name": True}


def _tenant_id(request: Request) -> str:
    ctx = getattr(request.state, "tenant", None)
    tid = getattr(ctx, "tenant_id", None) if ctx else None
    if not tid:
        raise HTTPException(401, "no tenant context")
    return tid


def create_disposition_router(db_pool, signing_identity, ledger_pool=None,
                              decision_trail=None) -> APIRouter:
    router = APIRouter(prefix="/v1/dispositions", tags=["Dispositions"])
    _writer = ledger_pool or db_pool  # ledger-role writer; single-role falls back to the main pool

    def _load_registry() -> dict:
        return _PROFILE_REGISTRY

    def _issuer_key_id() -> str:
        return "ed25519:" + signing_identity.public_key().hex()

    @router.post("", status_code=201)
    async def post_disposition(body: DispositionIn, request: Request):
        require_scope(request, "disposition:write")   # admin `*` bypasses
        if signing_identity is None:
            raise HTTPException(503, "disposition attest unavailable: runtime signing identity not configured")
        tenant_id = _tenant_id(request)

        # Decision 2: POST accepts the approver-signed inner ONLY.
        if body.system_signature is not None or body.system_metadata is not None:
            raise HTTPException(422, "system signature/metadata must not be submitted; the runtime counter-signs")

        rec = {"schema": body.schema_, "profile": body.profile, "approval_mode": body.approval_mode,
               "content_body": body.content_body, "approval_assertion": body.approval_assertion,
               "approver_signature": body.approver_signature}
        a = rec["approval_assertion"]
        if not isinstance(a, dict):
            raise HTTPException(422, "approval_assertion required")

        # Profile resolution (§8 step 2) from the RATIFIED registry.
        registry = _load_registry()
        entry = registry.get("profiles", {}).get(body.profile)
        if entry is None:
            raise HTTPException(422, f"unknown profile: {body.profile}")
        if body.approval_mode != entry.get("approval_mode"):
            raise HTTPException(422, "approval_mode mismatch")

        # Content integrity (§2.1) + approver signature (§2.2/§3.1). cgr.disposition.v1 is
        # REQUIRED unconditional, so the approver signature must be present and valid.
        if a.get("content_digest") != cv._content_digest(body.content_body):
            raise HTTPException(422, "content_digest mismatch")
        if not body.approver_signature:
            raise HTTPException(422, "approver signature required but absent")
        if not cv._ed25519_ok(a.get("approver_key_id"), body.approver_signature,
                              cv.DOMAIN_TAG + cv._jcs(a)):
            raise HTTPException(422, "approver signature invalid")

        approver_key_id = a.get("approver_key_id") or ""
        approver_hex = approver_key_id.rsplit(":", 1)[-1]
        record_nonce = a.get("record_nonce")
        if not record_nonce:
            raise HTTPException(422, "record_nonce required (replay prevention, spec §4)")

        # Approver-enrolled check (decision 4): the approver key MUST be an active approver for the
        # tenant; empty registry → reject. Read the enrolment's assurance tier (never self-claimed).
        with db_pool.connection() as conn:
            row = conn.execute(
                "SELECT assurance FROM hitl_approvers WHERE tenant_id = %s AND public_key = %s AND active = TRUE",
                (tenant_id, approver_hex)).fetchone()
        if row is None:
            raise HTTPException(403, "approver_not_enrolled: approver key is not an active approver for this tenant")
        assurance = (row["assurance"] if isinstance(row, dict) else row[0]) or "none"

        # Counter-sign (§3.2): the runtime issuer signs the body INCLUDING approver_signature.
        rec["system_metadata"] = {"issuer": "grafomem-runtime", "issuer_key_id": _issuer_key_id(),
                                  "recorded_at": datetime.now(timezone.utc).isoformat()}
        body_bytes = cv._jcs({k: v for k, v in rec.items() if k not in _ENVELOPE_KEYS})
        sig, _pub = signing_identity.sign(body_bytes)
        rec["system_signature"] = "ed25519-sig:" + sig.hex()

        # Defense in depth: the assembled record MUST verify offline under the pinned issuer.
        res = cv.verify(rec, registry, trusted_issuers={signing_identity.public_key().hex()})
        if not res.get("valid"):
            logger.error("dispositions: self-verify failed after counter-sign: %s", res)
            raise HTTPException(500, "internal: assembled record failed self-verification")

        record_id = uuid.uuid4().hex
        try:
            with _writer.connection() as conn:
                conn.execute(
                    "INSERT INTO cosign_dispositions (record_id, tenant_id, profile, approval_mode, "
                    " content_body, content_digest, approval_assertion, approver_signature, "
                    " approver_key_id, record_nonce, system_metadata, system_signature, issuer_key_id, assurance) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (record_id, tenant_id, body.profile, body.approval_mode,
                     json.dumps(body.content_body), a.get("content_digest"),
                     json.dumps(a), body.approver_signature, approver_key_id, record_nonce,
                     json.dumps(rec["system_metadata"]), rec["system_signature"],
                     rec["system_metadata"]["issuer_key_id"], assurance))
        except Exception as e:  # UNIQUE (approver_key_id, record_nonce) → replay
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                raise HTTPException(409, "record_nonce replay: (approver_key_id, record_nonce) already recorded")
            raise

        # Trail: the append-only cosign_dispositions row above IS the disposition's signed,
        # tenant-scoped governed trail. A UNIFIED entry in decision_trail is deferred: DecisionTrail.log
        # is inference-shaped (query/model_id/raw_output) and would need a disposition→trail field
        # mapping to avoid writing misleading fields — flagged for the operator (see the PR/report).
        return {"record_id": record_id, "issuer_key_id": rec["system_metadata"]["issuer_key_id"],
                "assurance": assurance}

    @router.get("/{record_id}")
    async def get_disposition(record_id: str, request: Request):
        require_scope(request, "disposition:read")
        tenant_id = _tenant_id(request)
        with db_pool.connection() as conn:
            row = conn.execute(
                "SELECT record_id, tenant_id, profile, approval_mode, content_body, content_digest, "
                " approval_assertion, approver_signature, system_metadata, system_signature, "
                " issuer_key_id, assurance, created_at "
                "FROM cosign_dispositions WHERE record_id = %s AND tenant_id = %s",
                (record_id, tenant_id)).fetchone()
        if row is None:
            raise HTTPException(404, "disposition not found")
        d = dict(row) if isinstance(row, dict) else None
        # Return the full cgr.cosign.v1 envelope so a client can verify OFFLINE with the public verifier.
        envelope = {
            "schema": "cgr.cosign.v1", "profile": d["profile"], "approval_mode": d["approval_mode"],
            "content_body": d["content_body"], "approval_assertion": d["approval_assertion"],
            "approver_signature": d["approver_signature"], "system_metadata": d["system_metadata"],
            "system_signature": d["system_signature"], "evidence_ref": None,
        }
        return {"record_id": d["record_id"], "assurance": d["assurance"],  # assurance SURFACED, never gated
                "issuer_key_id": d["issuer_key_id"], "created_at": d["created_at"], "record": envelope}

    return router
