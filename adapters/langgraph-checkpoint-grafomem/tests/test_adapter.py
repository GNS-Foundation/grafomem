import pytest
import time
import copy
from typing import TypedDict, Annotated
import operator

from cryptography.hazmat.primitives.asymmetric import ed25519
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from grafomem.errors import SignatureMismatch

from grafomem_checkpoint.serializer import GrafomemSerializer
from grafomem_checkpoint.saver import GrafomemCheckpointSaver, EMPTY_THREAD_DIGEST
from grafomem_checkpoint.receipt_log import (
    InMemoryReceiptLog,
    FileReceiptLog,
    verify_log,
)


class State(TypedDict):
    messages: Annotated[list, operator.add]
    count: int
    large_data: list[dict]

def my_node(state: State):
    return {"messages": ["hello"], "count": state.get("count", 0) + 1, "large_data": state.get("large_data", [])}

async def my_async_node(state: State):
    return {"messages": ["hello"], "count": state.get("count", 0) + 1, "large_data": state.get("large_data", [])}

def build_graph(saver):
    builder = StateGraph(State)
    builder.add_node("A", my_node)
    builder.add_node("B", my_node)
    builder.add_edge(START, "A")
    builder.add_edge("A", "B")
    builder.add_edge("B", END)
    return builder.compile(checkpointer=saver)

def test_real_graph_interrupt_and_resume():
    priv = ed25519.Ed25519PrivateKey.generate()
    serde = GrafomemSerializer(private_key=priv)
    inner = MemorySaver(serde=serde)
    saver = GrafomemCheckpointSaver(inner)
    
    # Graph that suspends / interrupts
    builder = StateGraph(State)
    builder.add_node("A", my_node)
    builder.add_edge(START, "A")
    builder.add_edge("A", END)
    graph = builder.compile(checkpointer=saver, interrupt_after=["A"])
    
    config = {"configurable": {"thread_id": "thread-1"}}
    
    # Invoke and interrupt
    result1 = graph.invoke({"messages": ["start"], "count": 0, "large_data": [{"key": "value"}] * 10}, config)
    
    # State should be at A
    checkpoint_tuple = saver.get_tuple(config)
    assert checkpoint_tuple is not None
    assert checkpoint_tuple.checkpoint["id"] is not None
    assert checkpoint_tuple.metadata.get("grafomem_content_hash") is not None
    
    # Resume bit-identical
    result2 = graph.invoke(None, config)
    assert result2["count"] == 1
    assert result2["messages"] == ["start", "hello"]
    
@pytest.mark.asyncio
async def test_async_real_graph_interrupt_resume_adelete():
    priv = ed25519.Ed25519PrivateKey.generate()
    serde = GrafomemSerializer(private_key=priv)
    inner = MemorySaver(serde=serde)
    saver = GrafomemCheckpointSaver(inner, receipt_log=InMemoryReceiptLog())

    # Graph that suspends / interrupts
    builder = StateGraph(State)
    builder.add_node("A", my_async_node)
    builder.add_edge(START, "A")
    builder.add_edge("A", END)
    graph = builder.compile(checkpointer=saver, interrupt_after=["A"])

    config = {"configurable": {"thread_id": "thread-async-1"}}

    # ainvoke and interrupt
    await graph.ainvoke({"messages": ["start_async"], "count": 0, "large_data": []}, config)

    # State should be at A
    checkpoint_tuple = await saver.aget_tuple(config)
    assert checkpoint_tuple is not None
    assert checkpoint_tuple.checkpoint["id"] is not None
    assert checkpoint_tuple.metadata.get("grafomem_content_hash") is not None

    # Resume bit-identical
    result2 = await graph.ainvoke(None, config)
    assert result2["count"] == 1
    assert result2["messages"] == ["start_async", "hello"]

    # Async Delete Thread
    await saver.adelete_thread("thread-async-1")

    # Get receipt and verify: `before` now binds the thread's content digest
    # (a real 64-hex SHA-256), not the old literal "thread_data".
    rcpt = saver.last_receipt("thread-async-1")
    assert rcpt is not None
    assert rcpt.scope == "thread-async-1"
    assert rcpt.before != "thread_data"
    assert len(rcpt.before) == 64 and rcpt.before != EMPTY_THREAD_DIGEST
    assert rcpt.after == "erased"
    assert rcpt.verify(priv.public_key())
    # The erasure is recorded in the append-only log.
    assert len(saver.audit_log()) == 1
    
def test_tamper_signature_mismatch():
    priv = ed25519.Ed25519PrivateKey.generate()
    serde = GrafomemSerializer(private_key=priv)
    inner = MemorySaver(serde=serde)
    saver = GrafomemCheckpointSaver(inner)
    
    graph = build_graph(saver)
    config = {"configurable": {"thread_id": "thread-2"}}
    graph.invoke({"messages": ["hi"], "count": 0}, config)
    
    type_, gfm_bytes = serde.dumps_typed({"id": "some_id", "v": 1})
    
    # Flip a byte
    b_arr = bytearray(gfm_bytes)
    b_arr[-1] ^= 0x01
    
    with pytest.raises(ValueError, match="signature mismatch"):
        serde.loads_typed((type_, bytes(b_arr)))

def test_delete_thread_receipt():
    priv = ed25519.Ed25519PrivateKey.generate()
    serde = GrafomemSerializer(private_key=priv)
    inner = MemorySaver(serde=serde)
    saver = GrafomemCheckpointSaver(inner, receipt_log=InMemoryReceiptLog())

    graph = build_graph(saver)
    config = {"configurable": {"thread_id": "thread-3"}}
    graph.invoke({"messages": ["hi"], "count": 0}, config)

    # Erase
    saver.delete_thread("thread-3")

    # Get receipt
    rcpt = saver.last_receipt("thread-3")
    assert rcpt is not None
    assert rcpt.scope == "thread-3"
    # `before` binds a real content digest of what was erased, not a literal.
    assert rcpt.before != "thread_data"
    assert len(rcpt.before) == 64 and rcpt.before != EMPTY_THREAD_DIGEST
    assert rcpt.after == "erased"

    # Verify receipt signature
    assert rcpt.verify(priv.public_key())


def _mk_saver(priv, receipt_log):
    serde = GrafomemSerializer(private_key=priv)
    inner = MemorySaver(serde=serde)
    return GrafomemCheckpointSaver(inner, receipt_log=receipt_log)


def test_receipt_binds_thread_content():
    """Different thread contents produce different `before` digests, and an empty thread
    yields the empty-thread sentinel — i.e. the receipt commits to the specific data."""
    priv = ed25519.Ed25519PrivateKey.generate()
    saver = _mk_saver(priv, InMemoryReceiptLog())

    g = build_graph(saver)
    g.invoke({"messages": ["alpha"], "count": 0}, {"configurable": {"thread_id": "t-a"}})
    g.invoke({"messages": ["beta", "beta2"], "count": 5}, {"configurable": {"thread_id": "t-b"}})

    saver.delete_thread("t-a")
    saver.delete_thread("t-b")
    saver.delete_thread("t-never-existed")

    ra = saver.last_receipt("t-a")
    rb = saver.last_receipt("t-b")
    rn = saver.last_receipt("t-never-existed")

    assert ra.before != rb.before          # bound to distinct contents
    assert len(ra.before) == 64 and len(rb.before) == 64
    assert rn.before == EMPTY_THREAD_DIGEST  # nothing was there to bind
    assert ra.verify(priv.public_key()) and rb.verify(priv.public_key())


def test_append_only_log_persists_across_restart(tmp_path):
    """The erasure is in a durable append-only log that a *new* saver instance still sees
    — the direct answer to 'can an auditor tell a real deletion from delete_thread never
    being called?'."""
    priv = ed25519.Ed25519PrivateKey.generate()
    log_path = str(tmp_path / "receipts.jsonl")

    saver = _mk_saver(priv, FileReceiptLog(log_path))
    g = build_graph(saver)
    g.invoke({"messages": ["hi"], "count": 0}, {"configurable": {"thread_id": "t-1"}})
    g.invoke({"messages": ["yo"], "count": 0}, {"configurable": {"thread_id": "t-2"}})
    saver.delete_thread("t-1")
    saver.delete_thread("t-2")

    # Simulate a process restart: a brand-new saver over the same log file.
    saver2 = _mk_saver(priv, FileReceiptLog(log_path))
    entries = saver2.audit_log()
    assert [e["receipt"]["scope"] for e in entries] == ["t-1", "t-2"]
    assert entries[0]["seq"] == 0 and entries[1]["seq"] == 1

    # A further erasure continues the SAME chain, not a fresh one.
    g2 = build_graph(saver2)
    g2.invoke({"messages": ["hey"], "count": 0}, {"configurable": {"thread_id": "t-3"}})
    saver2.delete_thread("t-3")
    entries = saver2.audit_log()
    assert len(entries) == 3
    assert entries[2]["prev_entry_hash"] == entries[1]["entry_hash"]

    ok, problems = verify_log(entries, {saver2.inner.serde.key_id: priv.public_key()})
    assert ok, problems


def test_log_verifies_clean():
    priv = ed25519.Ed25519PrivateKey.generate()
    saver = _mk_saver(priv, InMemoryReceiptLog())
    g = build_graph(saver)
    for tid in ("x", "y", "z"):
        g.invoke({"messages": [tid], "count": 0}, {"configurable": {"thread_id": tid}})
        saver.delete_thread(tid)
    ok, problems = verify_log(saver.audit_log(), {saver.inner.serde.key_id: priv.public_key()})
    assert ok, problems


def test_tamper_breaks_receipt_verification():
    """Non-vacuity: prove the receipt guard actually fails on tamper (not vacuously True)."""
    priv = ed25519.Ed25519PrivateKey.generate()
    saver = _mk_saver(priv, InMemoryReceiptLog())
    g = build_graph(saver)
    g.invoke({"messages": ["hi"], "count": 0}, {"configurable": {"thread_id": "t"}})
    saver.delete_thread("t")
    rcpt = saver.last_receipt("t")

    # Clean receipt verifies (guard not vacuous)...
    assert rcpt.verify(priv.public_key()) is True
    # ...but altering the bound content digest breaks verification.
    import copy as _copy
    tampered = _copy.copy(rcpt)
    tampered.before = "0" * 64
    assert tampered.verify(priv.public_key()) is False
    # ...and a different key cannot verify it either.
    other = ed25519.Ed25519PrivateKey.generate().public_key()
    assert rcpt.verify(other) is False


def test_tamper_breaks_log_verification():
    """Non-vacuity: a mutated or reordered log entry is caught by verify_log."""
    priv = ed25519.Ed25519PrivateKey.generate()
    saver = _mk_saver(priv, InMemoryReceiptLog())
    g = build_graph(saver)
    for tid in ("a", "b"):
        g.invoke({"messages": [tid], "count": 0}, {"configurable": {"thread_id": tid}})
        saver.delete_thread(tid)
    keys = {saver.inner.serde.key_id: priv.public_key()}

    clean = saver.audit_log()
    assert verify_log(clean, keys)[0] is True  # not vacuous

    # Mutate a receipt field without fixing the hash/signature -> caught.
    import copy as _copy
    mutated = _copy.deepcopy(clean)
    mutated[0]["receipt"]["scope"] = "someone-else"
    ok, problems = verify_log(mutated, keys)
    assert ok is False and problems

    # Drop the first entry (so the chain no longer links) -> caught.
    ok2, problems2 = verify_log(clean[1:], keys)
    assert ok2 is False and problems2

def test_serialization_overhead():
    priv = ed25519.Ed25519PrivateKey.generate()
    serde = GrafomemSerializer(private_key=priv)
    inner = MemorySaver(serde=serde)
    saver = GrafomemCheckpointSaver(inner)
    
    # Generate large state
    large_data = [{"k": f"value_{i}", "data": "x" * 1000} for i in range(1000)] # ~1MB
    state = {"v": 1, "id": "test", "ts": "2026", "channel_values": {"large_data": large_data}}
    
    # Measure without Grafomem
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    base_serde = JsonPlusSerializer()
    
    t0 = time.time()
    for _ in range(10):
        base_serde.dumps_typed(state)
    base_time = (time.time() - t0) / 10
    
    t0 = time.time()
    for _ in range(10):
        serde.dumps_typed(state)
    grafomem_time = (time.time() - t0) / 10
    
    overhead = grafomem_time - base_time
    print(f"\n[Overhead Report] Base serialization: {base_time*1000:.2f}ms, Grafomem serialization: {grafomem_time*1000:.2f}ms. Overhead: {overhead*1000:.2f}ms per step.")
