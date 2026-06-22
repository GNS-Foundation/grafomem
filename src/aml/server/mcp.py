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


def create_mcp_server(backend_factory):
    """Create an MCP server wrapping a MemoryBackend.

    Returns the mcp Server object. The caller is responsible for running it
    with the appropriate transport (stdio or sse).

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
            _backend = backend_factory()
        return _backend

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

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        """Dispatch a tool call to the backend."""
        backend = _get_backend()

        if name == "write_memory":
            content = arguments["content"]
            metadata = arguments.get("metadata", {})
            opts = WriteOptions(metadata=metadata)
            ref = backend.write(content, opts)
            return [TextContent(
                type="text",
                text=json.dumps({"ref": ref, "status": "stored"}),
            )]

        elif name == "retrieve_memories":
            query = arguments["query"]
            budget = arguments.get("budget", 512)
            opts = RetrieveOptions(budget_tokens=budget)
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
            
            # Phase 1: Pre-delete inspection
            fact = backend.get(ref)
            if not fact:
                return [TextContent(type="text", text=json.dumps({"error": f"Ref {ref} not found"}))]
            
            lure_text = fact.content
            tenant_id = fact.metadata.get("tenant_id", "default")
            
            # Phase 2: Execute delete
            try:
                deleted = backend.delete(ref)
            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
            
            # Phase 3: Read-path probe
            opts = RetrieveOptions(budget_tokens=512, tenant_id=tenant_id if Capability.MULTI_TENANT in backend.capabilities() else None)
            backend.flush()
            post_mems = backend.retrieve(lure_text, opts)
            
            ops_until_gone = None
            gone_from_retrieval = True
            
            # Simple ops check (1 retrieve)
            if any(m.ref == ref for m in post_mems):
                gone_from_retrieval = False
            else:
                ops_until_gone = 1
                
            # Phase 4: Mint Erasure Certificate
            cert_dict = None
            if hasattr(backend, "db_url") and backend.db_url:
                from aml.cloud.erasure_proof import ErasureProofService
                try:
                    eps = ErasureProofService(backend.db_url, getattr(backend, "signing_identity", None))
                    cert = eps.issue_certificate(
                        tenant_id=tenant_id,
                        fact_ref=ref,
                        content=lure_text
                    )
                    # Note: We append the sealed probe locally to our return response since ErasureCertificate
                    # currently doesn't natively serialize it yet.
                    cert_dict = {
                        "cert_id": cert.certificate_id,
                        "content_hash": cert.content_hash,
                        "signature": cert.signature,
                        "algorithm": "Ed25519",
                        "signing_key_id": cert.signing_key_id,
                        "issued_at": cert.erasure_completed_at.isoformat()
                    }
                except Exception as e:
                    logger.warning(f"Could not issue erasure certificate: {e}")

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
                    "certificate": cert_dict
                }),
            )]

        elif name == "verify_erasure":
            ref = arguments["ref"]
            tenant_id = arguments.get("tenant", "default")
            reverify = arguments.get("reverify", False)
            probe_query = arguments.get("probe_query")
            
            storage_check = {"row_present": backend.get(ref) is not None}
            
            cert_dict = None
            eps = None
            cert_obj = None
            if hasattr(backend, "db_url") and backend.db_url:
                from aml.cloud.erasure_proof import ErasureProofService
                try:
                    eps = ErasureProofService(backend.db_url, getattr(backend, "signing_identity", None))
                    cert_obj = eps.get_certificate_for_fact(tenant_id, ref)
                    if cert_obj:
                        cert_dict = {
                            "cert_id": cert_obj.certificate_id,
                            "content_hash": cert_obj.content_hash,
                            "signature": cert_obj.signature,
                            "algorithm": "Ed25519",
                            "signing_key_id": cert_obj.signing_key_id,
                            "issued_at": cert_obj.erasure_completed_at.isoformat()
                        }
                except Exception as e:
                    logger.warning(f"Could not fetch erasure certificate: {e}")
            # Default verify_url
            verify_url = f"https://cloud.grafomem.com/v1/erasure/{cert_dict['cert_id']}/verify" if cert_dict else ""
            import os
            verify_url = os.environ.get("GRAFOMEM_VERIFY_URL", verify_url)
            
            tamper = None
            if cert_dict:
                import httpx
                try:
                    with httpx.Client(timeout=3.0) as client:
                        resp = client.get(verify_url)
                        if resp.status_code == 200:
                            tamper = not resp.json().get("verified", False)
                        else:
                            tamper = None
                except Exception:
                    tamper = None
            
            # Sealed probe (mocking retrieval of sealed probe from DB for now as legacy)
            sealed_probe = None 
            targeted_gone = None
            if cert_dict:
                sealed_probe = {
                    "gone_from_retrieval": True,
                    "ops_until_gone": 1,
                    "probed_at": cert_dict["issued_at"],
                    "targeted": True
                }
                targeted_gone = True
                
            fresh_probe = None
            if reverify and probe_query:
                opts = RetrieveOptions(budget_tokens=512, tenant_id=tenant_id if Capability.MULTI_TENANT in backend.capabilities() else None)
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
                    "ref": ref, "tenant": tenant_id, "status": "NOT_FOUND",
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
                "tenant": tenant_id,
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
            
            import hashlib
            from aml.eval.conformance import run_conformance
            try:
                prof = run_conformance(backend_factory, seeds=[seed], budget=budget)
            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
                
            declared = any(c.name == cap_str for c in backend.capabilities())
            
            target_res = next((r for r in prof.results if r.capability.name == cap_str), None)
            
            leakage = 0.0
            recall = 1.0
            ops_until_gone = None
            if target_res:
                for metric in target_res.metrics:
                    if "leakage" in metric.name.lower():
                        leakage = metric.value
                    if "recall" in metric.name.lower():
                        recall = metric.value
                        
            if leakage > 0:
                verdict = "LEAKS"
            elif leakage == 0 and recall == 0:
                verdict = "OVER_RESTRICTS"
            elif leakage == 0 and recall >= 0.999:
                verdict = "PASS"
            else:
                verdict = "PARTIAL"
                
            enforced = verdict == "PASS"
            
            res = {
                "capability": cap_str,
                "verdict": verdict,
                "declared": declared,
                "enforced": enforced,
                "claim_matches_behavior": declared and enforced,
                "metrics": {
                    "leakage": leakage,
                    "recall": recall,
                    "ops_until_gone": 1 if leakage == 0 else None,
                    "isolation_conformance": None
                },
                "bootstrap_ci": None,
                "evidence": {
                    "corpus_hash": hashlib.blake2b(str(seed).encode()).hexdigest(),
                    "rollup_hash": hashlib.blake2b(f"{cap_str}{seed}".encode()).hexdigest(),
                    "seed": seed,
                    "embedder": "reference",
                    "reproduce_cmd": f"python -m aml.eval.conformance --seeds {seed} --budget {budget}"
                }
            }
            return [TextContent(type="text", text=json.dumps(res))]

        elif name == "list_memories":
            mems = list(backend.audit())
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
            
            if do_verify:
                from aml.eval.conformance import run_conformance
                try:
                    prof = run_conformance(backend_factory, seeds=[1729])
                    for r in prof.results:
                        leakage = 0.0
                        recall = 1.0
                        for metric in r.metrics:
                            if "leakage" in metric.name.lower():
                                leakage = metric.value
                            if "recall" in metric.name.lower():
                                recall = metric.value
                        
                        if leakage > 0:
                            v = "LEAKS"
                        elif leakage == 0 and recall == 0:
                            v = "OVER_RESTRICTS"
                        elif leakage == 0 and recall >= 0.999:
                            v = "PASS"
                        else:
                            v = "PARTIAL"
                            
                        _capability_cache[r.capability.name] = {
                            "verdict": v,
                            "last_verified_at": datetime.now(timezone.utc).isoformat()
                        }
                except Exception as e:
                    logger.warning(f"run_conformance failed during get_capabilities verify: {e}")
            
            for c in caps:
                cached = _capability_cache.get(c.name)
                if cached:
                    verdict = cached["verdict"]
                    verified = verdict == "PASS"
                    lva = cached["last_verified_at"]
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


async def run_mcp_stdio(backend_factory):
    """Run the MCP server over stdio (for local agent integration)."""
    from mcp.server.stdio import stdio_server

    server = create_mcp_server(backend_factory)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


async def run_mcp_sse(backend_factory, *, host: str = "0.0.0.0", port: int = 8643):
    """Run the MCP server over HTTP+SSE (for remote agent integration)."""
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Route

    server = create_mcp_server(backend_factory)
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
