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
from grafomem_checkpoint.saver import GrafomemCheckpointSaver


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
    saver = GrafomemCheckpointSaver(inner)
    
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
    
    # Get receipt and verify
    rcpt = saver.last_receipt("thread-async-1")
    assert rcpt is not None
    assert rcpt.scope == "thread-async-1"
    assert rcpt.before == "thread_data"
    assert rcpt.after == "erased"
    assert rcpt.verify(priv.public_key())
    
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
    saver = GrafomemCheckpointSaver(inner)
    
    graph = build_graph(saver)
    config = {"configurable": {"thread_id": "thread-3"}}
    graph.invoke({"messages": ["hi"], "count": 0}, config)
    
    # Erase
    saver.delete_thread("thread-3")
    
    # Get receipt
    rcpt = saver.last_receipt("thread-3")
    assert rcpt is not None
    assert rcpt.scope == "thread-3"
    assert rcpt.before == "thread_data"
    assert rcpt.after == "erased"
    
    # Verify receipt signature
    assert rcpt.verify(priv.public_key())

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
