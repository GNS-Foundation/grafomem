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
                            "description": "Optional key-value metadata (e.g., subject, predicate).",
                            "default": {},
                        },
                    },
                    "required": ["content"],
                },
            ),
            Tool(
                name="retrieve_memories",
                description=(
                    "Search for memories relevant to a natural-language query. "
                    "Returns the most relevant memories within the token budget."
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
                    "Delete a memory by its reference ID. "
                    "Requires the backend to support HARD_DELETE."
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
                name="list_memories",
                description=(
                    "List all memories in the store (audit). "
                    "Returns every stored memory for inspection."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
            Tool(
                name="get_capabilities",
                description=(
                    "List the capabilities this memory backend supports "
                    "(e.g., audit, hard_delete, multi_tenant, bi_temporal)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
        ]

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
            try:
                deleted = backend.delete(ref)
            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
            return [TextContent(
                type="text",
                text=json.dumps({"deleted": deleted, "ref": ref}),
            )]

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
            caps = sorted(c.value for c in backend.capabilities())
            return [TextContent(
                type="text",
                text=json.dumps({"capabilities": caps}),
            )]

        else:
            return [TextContent(
                type="text",
                text=json.dumps({"error": f"Unknown tool: {name}"}),
            )]

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
