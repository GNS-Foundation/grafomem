"""com.grafomem/cgr-read — the REMOTE CGR read MCP server.

Streamable HTTP, MCP spec **2026-07-28** (stateless: no protocol-level sessions, no
GET stream). Hand-rolled: ONE POST endpoint speaking JSON-RPC — `initialize`,
`tools/list`, `tools/call` (+ `ping`). Our tools are fast request/response, so a
request is answered with `application/json` (no SSE needed); a notification returns
`202 Accepted`.

Design decisions (Phase 2, Cowork-approved):
  * **Bearer auth (2a)** — the tenant API key arrives as `Authorization: Bearer <key>`
    and is resolved by the EXISTING auth middleware (cloud mode already accepts Bearer),
    so `request.state.tenant` is populated before this handler runs. `tools/call`
    additionally requires the `cgr:read` scope. OAuth 2.1 is a later (public/2b) concern.
  * **Envelope equivalence** — the tools call the SAME `build_read_result` /
    `list_subject_domains` read-core as `GET /v1/cgr/read/attestation`, so the signed v3
    envelope is identical by construction. No new crypto, no scoring change.
  * **No blocking on the event loop** — the sync-psycopg read-core runs via
    `anyio.to_thread.run_sync` (Phase 2 Q2A).
  * **No per-read anchor** — inherited from the read-core (Phase 2 Q1).

Public/unauthenticated serving stays a HARD STOP until the boundary spec + Gap-1/Gap-2;
this endpoint requires a valid tenant key exactly like the REST read surface.
"""
from __future__ import annotations

import functools
import json

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from aml.cgr.attestation import CGR_ATTESTATION_SCHEMA
from aml.cgr.routes import (
    _VERIFIER_LIB,
    _VERIFY_RECIPE_URL,
    build_read_result,
    list_subject_domains,
)

# Versions we KNOW our stateless POST→JSON behavior complies with, ascending. ISO
# YYYY-MM-DD dates sort lexically, so string comparison is date comparison.
_SUPPORTED_SORTED = ["2025-03-26", "2025-06-18", "2026-07-28"]
_SUPPORTED_PROTOCOL_VERSIONS = set(_SUPPORTED_SORTED)
_PROTOCOL_VERSION = _SUPPORTED_SORTED[-1]  # our highest


def negotiate_protocol(client_ver: str | None) -> str:
    """Pick the initialize-response protocolVersion.

    Real clients lag: Claude Code (and most SDKs) offer an INTERMEDIATE revision
    (e.g. 2025-11-25) we don't claim. Echoing the client's version when we support it,
    else replying with our *highest* (2026-07-28), makes those clients disconnect —
    they don't speak 2026-07-28. Spec-correct negotiation is: reply with the highest
    version WE support that is **≤** the client's offer (they always support versions at
    or below what they offered). Missing offer ⇒ our latest; client older than all we
    support ⇒ our lowest (they'll likely disconnect, which is honest — we don't speak it).
    """
    if client_ver in _SUPPORTED_PROTOCOL_VERSIONS:      # exact match ⇒ echo
        return client_ver
    if not client_ver:
        return _SUPPORTED_SORTED[-1]
    at_or_below = [v for v in _SUPPORTED_SORTED if v <= client_ver]
    return at_or_below[-1] if at_or_below else _SUPPORTED_SORTED[0]
_SERVER_NAME = "com.grafomem/cgr-read"
_SERVER_VERSION = "0.1.0"

# JSON-RPC error codes (standard + a couple app-specific in the -320xx range).
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_ERR_FORBIDDEN = -32001        # missing scope
_ERR_UNAVAILABLE = -32002      # foundation issuer not available (503-equivalent)

_TOOLS = [
    {
        "name": "cgr_get_attestation",
        "title": "Get CGR attestation",
        "description": (
            "Read the Foundation-signed Capability-Grounded Reputation (CGR) attestation "
            "for a subject (agent). Returns a signed, offline-verifiable v3 envelope: the "
            "pooled score with its evidence mass and freshness, the requested capability "
            "domain and its own resolved-evidence count, the issuer, and verify "
            "instructions. Unknown subject or a domain with no captured evidence returns "
            "an explicit no_evidence result — never a default score."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "description": "Agent identity: a 64-hex public key, a did:key:… , or a handle (facet@territory).",
                },
                "domain": {
                    "type": "string",
                    "description": "Optional capability domain to match against captured evidence, e.g. 'deploy-verification'.",
                },
            },
            "required": ["subject"],
        },
        "annotations": {"title": "Get CGR attestation", "readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "cgr_list_domains",
        "title": "List a subject's capability domains",
        "description": (
            "List the distinct capability domains in which a subject has captured CGR "
            "evidence. Read-only; returns no scores."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Agent identity: 64-hex key, did:key:… , or handle."},
            },
            "required": ["subject"],
        },
        "annotations": {"title": "List capability domains", "readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "cgr_verify_instructions",
        "title": "How to verify a CGR attestation offline",
        "description": (
            "Return instructions and pointers for verifying a CGR attestation yourself, "
            "offline, against the pinned Foundation issuer key — so you never have to trust "
            "this server. Includes the verifier library, the recipe URL, and the pinned "
            "issuer public key."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "annotations": {"title": "Verify instructions", "readOnlyHint": True},
    },
]


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _tenant_id(request: Request):
    ctx = getattr(request.state, "tenant", None)
    return getattr(ctx, "tenant_id", None)


def _has_scope(request: Request, scope: str) -> bool:
    ctx = getattr(request.state, "tenant", None)
    scopes = getattr(ctx, "scopes", None) or []
    return scope in scopes or "*" in scopes


def _tool_result(obj: dict) -> dict:
    """MCP tools/call result: text content (universally supported) + structuredContent."""
    return {
        "content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}],
        "structuredContent": obj,
        "isError": False,
    }


def create_cgr_mcp_router(decision_trail, store_manager, foundation_identity) -> APIRouter:
    """Mount the remote CGR read MCP endpoint at POST /mcp on the app (same service).

    Auth is handled by the shared middleware (Bearer/X-API-Key → request.state.tenant);
    this router only enforces the cgr:read scope on tools/call. foundation_identity may be
    None (Foundation seed unset) → tools/call that need signing return an unavailable error.
    """
    router = APIRouter(tags=["CGR Read MCP"])

    async def _call_tool(request: Request, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}

        if name == "cgr_verify_instructions":
            pub = foundation_identity.public_key().hex() if foundation_identity is not None else None
            return _tool_result({
                "recipe_url": _VERIFY_RECIPE_URL,
                "lib": _VERIFIER_LIB,
                "issuer_pubkey": pub,
                "schema": CGR_ATTESTATION_SCHEMA,
                "steps": [
                    "Fetch the attestation via cgr_get_attestation (the `attestation` object).",
                    f"Verify it with {_VERIFIER_LIB} against the PINNED issuer pubkey (do not trust this server).",
                    "Bind: pass the subject_key as expectedKey; reject on any mismatch.",
                    "For key-rotation continuity, re-walk GET /v1/cgr/rotations yourself.",
                ],
            })

        # Data tools require the cgr:read scope and a live Foundation issuer.
        if not _has_scope(request, "cgr:read"):
            raise _RpcError(_ERR_FORBIDDEN, "missing required scope: cgr:read")
        if foundation_identity is None:
            raise _RpcError(_ERR_UNAVAILABLE, "CGR Foundation issuer not available (FOUNDATION_SIGNING_SEED unset)")
        tenant_id = _tenant_id(request)
        if not tenant_id:
            raise _RpcError(_ERR_FORBIDDEN, "authentication required")

        if name == "cgr_get_attestation":
            try:
                return _tool_result(await anyio.to_thread.run_sync(functools.partial(
                    build_read_result, decision_trail, store_manager, foundation_identity, tenant_id,
                    subject=str(args.get("subject", "") or ""),
                    domain=str(args.get("domain", "") or ""))))
            except ValueError as e:
                raise _RpcError(_INVALID_PARAMS, str(e))

        if name == "cgr_list_domains":
            try:
                return _tool_result(await anyio.to_thread.run_sync(functools.partial(
                    list_subject_domains, decision_trail, store_manager, tenant_id,
                    subject=str(args.get("subject", "") or ""))))
            except ValueError as e:
                raise _RpcError(_INVALID_PARAMS, str(e))

        raise _RpcError(_INVALID_PARAMS, f"unknown tool: {name!r}")

    async def _handle(request: Request, method: str, params: dict) -> dict:
        if method == "initialize":
            ver = negotiate_protocol((params or {}).get("protocolVersion"))
            return {
                "protocolVersion": ver,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
                "instructions": "Read Capability-Grounded Reputation attestations. Verify them offline against the pinned issuer key.",
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": _TOOLS}
        if method == "tools/call":
            return await _call_tool(request, params or {})
        raise _RpcError(_METHOD_NOT_FOUND, f"method not found: {method}")

    @router.post("/mcp")
    async def mcp_endpoint(request: Request):
        # DNS-rebinding protection (spec MUST): reject a present, non-HTTPS Origin.
        # Non-browser MCP clients typically send no Origin — that is allowed.
        origin = request.headers.get("origin")
        if origin and not origin.startswith("https://"):
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": _INVALID_REQUEST, "message": "invalid Origin"}},
                status_code=403)

        try:
            msg = await request.json()
        except Exception:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None, "error": {"code": _PARSE_ERROR, "message": "parse error"}},
                status_code=400)

        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            mid = msg.get("id") if isinstance(msg, dict) else None
            return JSONResponse(
                {"jsonrpc": "2.0", "id": mid, "error": {"code": _INVALID_REQUEST, "message": "invalid JSON-RPC request"}},
                status_code=400)

        # A notification (no "id") gets 202 Accepted, no body (spec).
        if "id" not in msg:
            return Response(status_code=202)

        mid = msg.get("id")
        try:
            result = await _handle(request, msg["method"], msg.get("params") or {})
        except _RpcError as e:
            return JSONResponse({"jsonrpc": "2.0", "id": mid, "error": {"code": e.code, "message": e.message}})
        return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": result})

    return router
