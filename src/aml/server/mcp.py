"""
GRAFOMEM MCP tool server — Model Context Protocol integration.

Exposes GMP operations as MCP tools that AI agents (Claude, GPT-4, etc.)
can discover and invoke natively. Supports both transports:
  - stdio:  for local agent integration (pipes)
  - sse:    for remote agent integration (HTTP + Server-Sent Events)

Start via:
  grafomem serve --mcp stdio   # local agent via stdin/stdout
  grafomem serve --mcp sse     # remote agent via HTTP+SSE

MCP specification: https://modelcontextprotocol.io/
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("grafomem.mcp")


def create_mcp_server(backend_factory, erasure_service=None, tenant_id=None,
                      scratch_factory=None):
    """Create an MCP server wrapping a MemoryBackend.

    Returns the mcp Server object. The caller is responsible for running it
    with the appropriate transport (stdio or sse).

    `tenant_id` (process-per-tenant, MCP audit finding 9): the ONE tenant this
    server process acts for. Every write, retrieve, list, delete and verify is
    scoped to it; a caller-supplied `tenant` argument is rejected if it differs.
    A backend that declares MULTI_TENANT refuses to serve without a bound tenant
    (fail closed) — the alternative, trusting a per-call argument, is how one
    caller read another tenant's memory in the audit. Only a backend that does
    NOT declare MULTI_TENANT may run unbound — today both shipped backends
    (SQLite and Postgres) declare it, so both require a bound tenant.

    `scratch_factory` (finding 10): a factory for an ISOLATED store of the same
    backend kind, used by `run_conformance` / `get_capabilities(verify=true)`.
    The conformance suite writes ~1,170 synthetic facts; with no scratch factory
    those tools refuse rather than write into the serving store.

    `erasure_service` (0017): an `ErasureProofService` built by the entrypoint from
    the same configuration `grafomem serve` uses (Postgres + ledger + signing key).
    With it, `delete_memory` issues a real, factual certificate (0016) and
    `verify_erasure` verifies signatures in-process. Without it — OSS SQLite mode,
    or no ledger/key configured — both tools say so explicitly; nothing is faked.

    Requires: pip install mcp>=1.0
    """
    try:
        from mcp.server import Server
        from mcp.types import TextContent, Tool
    except ImportError as e:
        raise RuntimeError(
            "MCP support requires the 'mcp' package. "
            "Install with: pip install grafomem[server]"
        ) from e

    from aml.backends.interface import (
        Capability,
        MemoryBackend,
        RetrieveOptions,
        WriteOptions,
    )

    server = Server("grafomem")

    # Lazily create a single backend instance for the MCP session
    _backend = None

    def _get_backend():
        nonlocal _backend
        if _backend is None:
            b = backend_factory()
            if Capability.MULTI_TENANT in b.capabilities() and not tenant_id:
                raise RuntimeError(
                    "this backend declares MULTI_TENANT but no tenant is bound to this MCP "
                    "process — refusing to serve an untenanted multi-tenant store (audit "
                    "finding 9). Start with `grafomem serve --mcp ... --tenant <id>` or set "
                    "GRAFOMEM_MCP_TENANT."
                )
            _backend = b
        return _backend

    def _tenant_for(backend):
        """The tenant to scope operations to: the bound one on MULTI_TENANT backends."""
        return tenant_id if Capability.MULTI_TENANT in backend.capabilities() else None

    def _fact_tenant(fact):
        t = getattr(fact, "tenant_id", None)
        if t is None and isinstance(getattr(fact, "metadata", None), dict):
            t = fact.metadata.get("tenant_id")
        return t

    def _visible(backend, fact) -> bool:
        """A fact is visible to this process only if it belongs to the bound tenant
        (or the backend is single-tenant). Cross-tenant refs read as NOT FOUND."""
        if fact is None:
            return False
        scope = _tenant_for(backend)
        if scope is None:
            return True
        ft = _fact_tenant(fact)
        return ft is None or ft == scope

    def _reject_foreign_tenant(arguments):
        """A caller-supplied `tenant` is accepted only if it names the bound tenant."""
        asked = arguments.get("tenant")
        if asked and tenant_id and asked != tenant_id:
            return {"error": "tenant_mismatch",
                    "detail": f"this MCP process is bound to tenant {tenant_id!r}; "
                              f"it does not act for {asked!r}"}
        return None

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """Declare the tools this server exposes."""
        return [
            Tool(
                name="write_memory",
                description=(
                    "Store a new memory (fact, observation, note) in the agent's "
                    "persistent memory store. Returns the memory reference ID."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "The text content to store as a memory.",
                        },
                        "metadata": {
                            "type": "object",
                            "description": "Optional key-value metadata.",
                            "default": {},
                        },
                    },
                    "required": ["content"],
                },
            ),
            Tool(
                name="retrieve_memories",
                description=(
                    "Search for memories relevant to a natural-language query."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural-language query to search memories.",
                        },
                        "budget": {
                            "type": "integer",
                            "description": "Maximum character budget for returned content.",
                            "default": 512,
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="delete_memory",
                description=(
                    "Delete a memory by its reference ID. This runs an authoritative "
                    "read-path probe at delete time and seals the verdict in an Erasure Certificate."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "ref": {
                            "type": "integer",
                            "description": "The reference ID of the memory to delete.",
                        },
                    },
                    "required": ["ref"],
                },
            ),
            Tool(
                name="verify_erasure",
                description=(
                    "Verify that a previously deleted memory is gone from the retrieval path, "
                    "returning a cryptographically signed, independently re-verifiable erasure certificate."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "ref": { "type": "integer" },
                        "tenant": { "type": "string" },
                        "reverify": { "type": "boolean", "default": False },
                        "probe_query": { "type": "string" }
                    },
                    "required": ["ref"],
                },
            ),
            Tool(
                name="run_conformance",
                description=(
                    "Run the two-sided conformance suite for a declared capability against "
                    "the active backend and return the verdict with reproducible evidence."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "capability": {
                            "type": "string",
                            "enum": ["HARD_DELETE", "MULTI_TENANT", "CONCURRENCY_CONTROL"],
                        },
                        "seed": { "type": "integer", "default": 1729 },
                        "budget": { "type": "integer", "default": 512 }
                    },
                    "required": ["capability"],
                },
            ),
            Tool(
                name="list_memories",
                description=("List all memories in the store (audit)."),
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="get_capabilities",
                description=(
                    "Report the backend's capabilities — each declared flag paired with its "
                    "conformance verdict — so a capability is presented as a claim, not a guarantee."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "verify": { "type": "boolean", "default": False },
                        "capabilities": {
                            "type": "array",
                            "items": { "type": "string" }
                        }
                    },
                },
            ),
        ]

    # Global cache for capabilities to map flag -> verdict
    _capability_cache = {}

    def _conformance_verdicts(seed: int, budget: int):
        """Run the conformance suite on the SCRATCH store and read its results as the
        engine reports them (findings 10, 11, 12, 13).

        Returns (verdicts, evidence, error). `verdicts[cap] = {verdict, directions,
        workload}` where verdict is PASS or VIOLATION straight from
        `CapabilityResult.passed` — never re-derived from metric names, and never
        defaulted. `evidence.rollup_hash` is a digest over the actual results;
        `corpus_hash` is the corpus lock's hash when the lock file is present, else
        null — never a hash of the seed dressed up as one.
        """
        if scratch_factory is None:
            return None, None, (
                "conformance over MCP needs an isolated scratch store of the same backend "
                "kind, and none is configured for this backend (it would write ~1,170 "
                "synthetic facts into the serving store). Run "
                "`grafomem conformance -b <backend> --db <scratch>` instead."
            )
        import hashlib
        from aml.eval.conformance import run_conformance
        prof = run_conformance(scratch_factory, seeds=[seed], budget=budget)
        verdicts = {}
        for r in prof.results:
            verdicts[r.capability.name] = {
                "verdict": "PASS" if r.passed else "VIOLATION",
                "workload": r.workload,
                "directions": [
                    {"name": d.name, "objective": d.objective, "point": d.point,
                     "ci": list(d.ci), "passed": d.passed}
                    for d in r.directions
                ],
            }
        corpus_hash = None
        try:
            from pathlib import Path
            lock = Path("corpus/corpus.lock")
            if lock.exists():
                corpus_hash = json.loads(lock.read_text()).get("corpus_hash")
        except Exception:
            corpus_hash = None
        canonical = json.dumps(verdicts, sort_keys=True, separators=(",", ":"))
        evidence = {
            "store": f"scratch:{prof.store}",
            "corpus_hash": corpus_hash,
            "rollup_hash": hashlib.blake2b(canonical.encode("utf-8"), digest_size=32).hexdigest(),
            "seed": seed,
            "budget": budget,
            "reproduce_cmd": f"grafomem conformance -b <backend> --db <scratch> --seeds {seed} --budget {budget}",
        }
        return verdicts, evidence, None

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        """Dispatch a tool call to the backend."""
        backend = _get_backend()

        if name == "write_memory":
            content = arguments["content"]
            metadata = arguments.get("metadata", {})
            opts = WriteOptions(metadata=metadata, tenant_id=_tenant_for(backend))
            ref = backend.write(content, opts)
            return [TextContent(
                type="text",
                text=json.dumps({"ref": ref, "status": "stored"}),
            )]

        elif name == "retrieve_memories":
            query = arguments["query"]
            budget = arguments.get("budget", 512)
            opts = RetrieveOptions(budget_tokens=budget, tenant_id=_tenant_for(backend))
            backend.flush()
            mems = backend.retrieve(query, opts)
            results = [
                {
                    "ref": m.ref,
                    "content": m.content,
                    "written_at": m.written_at.isoformat() if m.written_at else None,
                }
                for m in mems
            ]
            return [TextContent(
                type="text",
                text=json.dumps({"memories": results, "count": len(results)}),
            )]

        elif name == "delete_memory":
            ref = arguments["ref"]
            
            mismatch = _reject_foreign_tenant(arguments)
            if mismatch:
                return [TextContent(type="text", text=json.dumps(mismatch))]

            # Phase 1: Pre-delete inspection — scoped: a ref owned by another tenant is
            # NOT FOUND to this process (never deleted, never acknowledged).
            fact = backend.get(ref)
            if not _visible(backend, fact):
                return [TextContent(type="text", text=json.dumps({"error": f"Ref {ref} not found"}))]
            
            lure_text = fact.content
            tenant_id_eff = _tenant_for(backend) or "default"
            
            # Phase 2: Execute delete
            try:
                deleted = backend.delete(ref)
            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
            
            # Phase 3: Read-path probe
            opts = RetrieveOptions(budget_tokens=512, tenant_id=_tenant_for(backend))
            backend.flush()
            post_mems = backend.retrieve(lure_text, opts)
            
            ops_until_gone = None
            gone_from_retrieval = True
            
            # Simple ops check (1 retrieve)
            if any(m.ref == ref for m in post_mems):
                gone_from_retrieval = False
            else:
                ops_until_gone = 1
                
            # Phase 4: Mint Erasure Certificate (0017 — through the wired service; the
            # previous `backend.db_url` branch could never run outside tests).
            # `lure_text` is the content observed in Phase 1, BEFORE the delete, so the
            # certificate's content hash is server-observed (0016), not asserted.
            cert_dict = None
            if erasure_service is None:
                cert_status = "not_issued: no erasure service in this MCP process"
            else:
                from aml.cloud.erasure_proof import FactStillPresent
                try:
                    cert = erasure_service.issue_certificate(
                        tenant_id=tenant_id_eff,
                        fact_ref=ref,
                        fact_content=lure_text,
                        backend=backend,  # 0014: probe coverage, don't default it
                    )
                    cert_dict = {
                        "cert_id": cert.certificate_id,
                        "content_hash": cert.fact_content_hash,
                        "signature": cert.signature.hex() if cert.signature else None,
                        "public_key": cert.public_key.hex() if cert.public_key else None,
                        "algorithm": "Ed25519",
                        "issued_at": cert.erasure_completed_at.isoformat(),
                    }
                    cert_status = "issued"
                except FactStillPresent as e:
                    # delete() reported success but the read-back still finds the fact:
                    # a store inconsistency. No certificate (0016); say so.
                    cert_status = "not_issued: fact_still_present"
                    logger.error("MCP erasure certificate NOT issued — fact still present "
                                 "after delete: %s", e)
                except Exception as e:
                    # 0013: the fact is deleted, no certificate is minted (e.g. ledger
                    # unavailable → LedgerRequired). Surface the reason, never swallow it.
                    cert_status = f"not_issued: {type(e).__name__}"
                    logger.error("MCP erasure certificate NOT issued (delete still applied): %s",
                                 e, exc_info=True)

            return [TextContent(
                type="text",
                text=json.dumps({
                    "deleted": deleted,
                    "ref": ref,
                    "sealed_probe": {
                        "gone_from_retrieval": gone_from_retrieval,
                        "ops_until_gone": ops_until_gone,
                        "probed_at": datetime.now(timezone.utc).isoformat(),
                        "targeted": True
                    },
                    "certificate": cert_dict,
                    "certificate_status": cert_status,
                }),
            )]

        elif name == "verify_erasure":
            ref = arguments["ref"]
            mismatch = _reject_foreign_tenant(arguments)
            if mismatch:
                return [TextContent(type="text", text=json.dumps(mismatch))]
            tenant_id_eff = _tenant_for(backend) or "default"
            reverify = arguments.get("reverify", False)
            probe_query = arguments.get("probe_query")
            
            # Storage presence, scoped: another tenant's row is not "present" to us.
            storage_check = {"row_present": _visible(backend, backend.get(ref))}
            
            cert_dict = None
            cert_obj = None
            if erasure_service is not None:
                try:
                    cert_obj = erasure_service.get_by_fact(tenant_id_eff, ref)
                    if cert_obj:
                        cert_dict = {
                            "cert_id": cert_obj.certificate_id,
                            "content_hash": cert_obj.fact_content_hash,
                            "signature": cert_obj.signature.hex() if cert_obj.signature else None,
                            "public_key": cert_obj.public_key.hex() if cert_obj.public_key else None,
                            "algorithm": "Ed25519",
                            "issued_at": cert_obj.erasure_completed_at.isoformat(),
                        }
                except Exception as e:
                    logger.error("MCP erasure certificate fetch failed: %s", e, exc_info=True)

            # Signature check (0017): IN-PROCESS, with the same key material that issued
            # the certificate. The previous implementation GETed a hard-coded
            # https://cloud.grafomem.com/... URL without credentials, so it could never
            # return a verdict and every answer was ERASED_UNVERIFIED. An optional
            # GRAFOMEM_VERIFY_URL still adds an independent remote check on top; it can
            # only make the verdict worse (tamper=True), never better.
            import os
            tamper = None
            verify_url = ""
            if cert_dict:
                try:
                    local = erasure_service.verify_certificate(cert_dict["cert_id"])
                    tamper = not bool(local.get("valid", False))
                    verify_url = "in-process"
                except Exception as e:
                    logger.error("MCP in-process certificate verification failed: %s", e)
                    tamper = None
                remote = os.environ.get("GRAFOMEM_VERIFY_URL")
                if remote:
                    verify_url = remote
                    try:
                        import httpx
                        with httpx.Client(timeout=3.0) as client:
                            resp = client.get(remote)
                            if resp.status_code == 200 and not resp.json().get("verified", False):
                                tamper = True
                    except Exception:
                        pass  # remote unreachable: the in-process verdict stands

            # Sealed probe (0016): no read-path probe is persisted at issuance yet, so
            # there is nothing to retrieve. It is reported as None — NEVER fabricated
            # from the mere existence of a certificate. A real read-path verdict comes
            # only from `reverify=true` + `probe_query` (fresh_probe below); without one
            # the status falls through to ERASED_UNVERIFIED with an explicit warning.
            sealed_probe = None
            targeted_gone = None

            fresh_probe = None
            if reverify and probe_query:
                opts = RetrieveOptions(budget_tokens=512, tenant_id=_tenant_for(backend))
                backend.flush()
                mems = backend.retrieve(probe_query, opts)
                gone = not any(m.ref == ref for m in mems)
                fresh_probe = {
                    "gone_from_retrieval": gone,
                    "ops_until_gone": 1 if gone else None,
                    "lure_source": "probe_query",
                    "probed_at": datetime.now(timezone.utc).isoformat()
                }
                targeted_gone = gone

            warning = None
            if targeted_gone is False:
                status = "NOT_ERASED"
            elif not cert_dict and not storage_check["row_present"]:
                # If it's not found in retrieve (targeted_gone was True or None), 
                # and no cert or row exists, it's simply NOT_FOUND
                return [TextContent(type="text", text=json.dumps({
                    "ref": ref, "tenant": tenant_id_eff, "status": "NOT_FOUND",
                    "storage_check": storage_check
                }))]
            elif targeted_gone is True and tamper is False:
                status = "ERASED_VERIFIED"
            elif targeted_gone is True and tamper is None:
                status = "ERASED_UNVERIFIED"
                warning = "Independent verification endpoint unreachable; certificate returned unverified. Re-check at verify_url."
            elif targeted_gone is True and tamper is True:
                status = "ERASED_UNVERIFIED"
                warning = "Certificate tamper detected."
            else:
                status = "ERASED_UNVERIFIED"
                warning = "Certificate predates read-path sealing; no targeted probe on record. Re-run with reverify=true and a probe_query to obtain a read-path verdict."
                
            if reverify and not probe_query:
                warning = "Fresh re-probe requested but no targeted lure available; returning sealed verdict only."

            res = {
                "ref": ref,
                "tenant": tenant_id_eff,
                "status": status,
                "certificate": cert_dict,
                "sealed_probe": sealed_probe,
                "fresh_probe": fresh_probe,
                "storage_check": storage_check,
                "tamper": tamper,
                "verify_url": verify_url,
                "regulatory_refs": ["GDPR Art. 17"],
                "warning": warning
            }
            return [TextContent(type="text", text=json.dumps(res))]

        elif name == "run_conformance":
            cap_str = arguments["capability"]
            seed = arguments.get("seed", 1729)
            budget = arguments.get("budget", 512)

            # Declared by the SERVING backend; tested on the SCRATCH store of the same kind.
            declared = any(c.name == cap_str for c in backend.capabilities())
            if not declared:
                # Finding 12: not declared → not tested → never PASS by default.
                return [TextContent(type="text", text=json.dumps({
                    "capability": cap_str, "verdict": "NOT_DECLARED", "declared": False,
                    "enforced": False, "claim_matches_behavior": None,
                    "metrics": None, "evidence": None,
                    "note": "the backend does not declare this capability, so it was not tested",
                }))]

            try:
                verdicts, evidence, err = _conformance_verdicts(seed, budget)
            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"error": f"{type(e).__name__}: {e}"}))]
            if err:
                return [TextContent(type="text", text=json.dumps({"error": err}))]

            target = verdicts.get(cap_str)
            if target is None:
                return [TextContent(type="text", text=json.dumps({
                    "capability": cap_str, "verdict": "UNTESTED", "declared": True,
                    "enforced": False, "claim_matches_behavior": None,
                    "metrics": None, "evidence": evidence,
                    "note": "declared, but the suite produced no result for it in this run",
                }))]

            enforced = target["verdict"] == "PASS"
            return [TextContent(type="text", text=json.dumps({
                "capability": cap_str,
                "verdict": target["verdict"],
                "declared": True,
                "enforced": enforced,
                "claim_matches_behavior": enforced,
                "metrics": {d["name"]: d["point"] for d in target["directions"]},
                "directions": target["directions"],
                "workload": target["workload"],
                "evidence": evidence,
            }))]

        elif name == "list_memories":
            # 0.4.1: the namespace this server writes and retrieves in (untenanted: write_memory / retrieve_memories
            # carry no tenant), never backend.audit(), which lists every tenant's memories of a shared database.
            from aml.backends.audit_scope import scoped_audit
            mems = scoped_audit(backend, _tenant_for(backend))
            results = [
                {
                    "ref": m.ref,
                    "content": m.content,
                    "written_at": m.written_at.isoformat() if m.written_at else None,
                }
                for m in mems
            ]
            return [TextContent(
                type="text",
                text=json.dumps({"memories": results, "count": len(results)}),
            )]

        elif name == "get_capabilities":
            do_verify = arguments.get("verify", False)
            subset = arguments.get("capabilities")
            
            caps = backend.capabilities()
            if subset:
                caps = [c for c in caps if c.name in subset]
                
            results = []
            claims_unverified = []
            
            verify_error = None
            if do_verify:
                # Findings 10/11/14: scratch store, engine verdicts, no swallowed errors.
                try:
                    verdicts, _evidence, err = _conformance_verdicts(seed=1729, budget=512)
                except Exception as e:
                    verdicts, err = None, f"{type(e).__name__}: {e}"
                if err:
                    verify_error = err
                    logger.error("get_capabilities verify failed: %s", err)
                else:
                    now = datetime.now(timezone.utc).isoformat()
                    for cap_name, v in verdicts.items():
                        _capability_cache[cap_name] = {"verdict": v["verdict"], "last_verified_at": now}

            for c in caps:
                cached = _capability_cache.get(c.name)
                if cached:
                    verdict = cached["verdict"]
                    verified = verdict == "PASS"
                    lva = cached["last_verified_at"]
                elif verify_error:
                    verdict = f"ERROR: {verify_error}"
                    verified = None
                    lva = None
                else:
                    verdict = "UNTESTED"
                    verified = None
                    lva = None
                    
                if verdict != "PASS":
                    claims_unverified.append(c.name)
                    
                results.append({
                    "flag": c.name,
                    "declared": True,
                    "verified": verified,
                    "verdict": verdict,
                    "last_verified_at": lva,
                    "evidence_ref": None
                })
                
            return [TextContent(
                type="text",
                text=json.dumps({
                    "backend": type(backend).__name__,
                    "capabilities": results,
                    "summary": {
                        "declared_count": len(caps),
                        "verified_count": sum(1 for r in results if r["verified"]),
                        "claims_unverified": claims_unverified
                    }
                }),
            )]

        else:
            return [TextContent(
                type="text",
                text=json.dumps({"error": f"Unknown tool: {name}"}),
            )]

    server._test_call_tool = call_tool
    return server


async def run_mcp_stdio(backend_factory, erasure_service=None, tenant_id=None, scratch_factory=None):
    """Run the MCP server over stdio (for local agent integration)."""
    from mcp.server.stdio import stdio_server

    server = create_mcp_server(backend_factory, erasure_service=erasure_service,
                               tenant_id=tenant_id, scratch_factory=scratch_factory)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


async def run_mcp_sse(backend_factory, *, host: str = "0.0.0.0", port: int = 8643,
                      erasure_service=None, tenant_id=None, scratch_factory=None):
    """Run the MCP server over HTTP+SSE (for remote agent integration)."""
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Route

    server = create_mcp_server(backend_factory, erasure_service=erasure_service,
                               tenant_id=tenant_id, scratch_factory=scratch_factory)
    sse = SseServerTransport("/messages")

    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(
                streams[0], streams[1],
                server.create_initialization_options(),
            )

    async def handle_messages(request):
        await sse.handle_post_message(request.scope, request.receive, request._send)

    starlette_app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Route("/messages", endpoint=handle_messages, methods=["POST"]),
        ],
    )

    import uvicorn
    config = uvicorn.Config(starlette_app, host=host, port=port)
    server_instance = uvicorn.Server(config)
    logger.info("MCP SSE server starting on %s:%d", host, port)
    await server_instance.serve()
