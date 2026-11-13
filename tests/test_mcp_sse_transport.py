"""MCP over SSE: the transport must survive more than one message.

Bug (0.5.0, reported 2026-11-13 from the Valmerra demo behind agentgateway): both Starlette
route endpoints in run_mcp_sse returned None. Starlette then awaited the missing response
(`await response(scope, receive, send)`) → `TypeError: 'NoneType' object is not callable`
on every message, uvicorn dropped the connection, and the Python MCP client hung on its
next message ("Error in post_writer" / httpx.ReadError).

The test runs the real SSE server in-process, opens three client sessions in a row with
two tool calls each, and asserts they all finish within a timeout and that no TypeError
reached the log. Against the 0.5.0 routes it hangs on the first session (the timeout
fires) and the TypeError is logged.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time

import pytest

from aml.backends.vector_only import _stub_embedder

pytestmark = pytest.mark.asyncio


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(self.format(record) + (("\n" + self.formatException(record.exc_info)) if record.exc_info else ""))


@pytest.fixture
def sse_server(tmp_path):
    """The real run_mcp_sse, on a loopback port, in a thread with its own loop."""
    from aml.backends.sqlite_gmp import SQLiteGMPBackend
    from aml.server.mcp import run_mcp_sse

    batch = _stub_embedder(64)
    embed = lambda t: batch([t])[0] if isinstance(t, str) else batch(t)
    factory = lambda: SQLiteGMPBackend(db_path=str(tmp_path / "sse.db"), embed_fn=embed)
    port = _free_port()

    cap = _Capture()
    cap.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    for name in ("uvicorn.error", "uvicorn", "starlette", "mcp"):
        logging.getLogger(name).addHandler(cap)

    loop = asyncio.new_event_loop()
    done = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_mcp_sse(factory, host="127.0.0.1", port=port, tenant_id="t"))
        except Exception:
            pass
        finally:
            done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", cap
    for name in ("uvicorn.error", "uvicorn", "starlette", "mcp"):
        logging.getLogger(name).removeHandler(cap)
    loop.call_soon_threadsafe(loop.stop)


async def _one_session(url: str, i: int) -> list[int]:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    import json
    async with sse_client(url + "/sse") as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        a = json.loads((await s.call_tool("write_memory", {"content": f"session {i} fact"})).content[0].text)["ref"]
        got = json.loads((await s.call_tool("retrieve_memories", {"query": f"session {i} fact"})).content[0].text)
        assert any(m["ref"] == a for m in got["memories"])
        return [a]


async def test_three_sessions_two_calls_each_complete_and_no_typeerror(sse_server):
    url, cap = sse_server
    refs = []
    for i in range(3):
        refs += await asyncio.wait_for(_one_session(url, i), timeout=15)
    assert len(refs) == 3
    bad = [r for r in cap.records if "TypeError" in r or "NoneType" in r or "Exception in ASGI application" in r]
    assert not bad, "server logged a transport error:\n" + "\n".join(bad[:3])
