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
from grafomem_checkpoint.thread_keys import (
    ThreadKeyManager,
    InMemoryKeyStore,
    ThreadKeyDestroyed,
    ThreadContextMissing,
    current_thread_id,
)

MASTER_KEY_HEX = "ab" * 32  # 64 hex chars = 32 bytes


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


# ============================================================================
# v1.2.0 — per-thread crypto-erasure
# ============================================================================

def _mk_enc_saver(priv, km, receipt_log):
    serde = GrafomemSerializer(private_key=priv, key_manager=km)
    inner = MemorySaver(serde=serde)
    return GrafomemCheckpointSaver(inner, receipt_log=receipt_log), serde


def test_encryption_ciphertext_at_rest_and_roundtrip():
    """With a key_manager, the serialized blob is ciphertext (the plaintext marker is absent),
    and it round-trips. Without a key_manager it is plaintext (marker present) — so the
    non-encrypted path is non-vacuous."""
    priv = ed25519.Ed25519PrivateKey.generate()
    km = ThreadKeyManager(MASTER_KEY_HEX, key_store=InMemoryKeyStore())
    enc = GrafomemSerializer(private_key=priv, key_manager=km)
    plain = GrafomemSerializer(private_key=priv)  # no key_manager

    marker = {"note": "plaintext-canary-value"}

    # plaintext path: marker IS present in the bytes
    _, plain_bytes = plain.dumps_typed(marker)
    assert b"plaintext-canary-value" in plain_bytes

    # encrypted path (thread in scope): marker is NOT present, but round-trips
    tok = current_thread_id.set("t-enc")
    try:
        type_, enc_bytes = enc.dumps_typed(marker)
        assert b"plaintext-canary-value" not in enc_bytes
        assert enc.loads_typed((type_, enc_bytes)) == marker
    finally:
        current_thread_id.reset(tok)


def test_crypto_erasure_makes_retained_ciphertext_undecryptable():
    """Headline + non-vacuity: a retained copy of a thread's ciphertext decrypts BEFORE the
    key is destroyed and FAILS after — proving delete_thread destroyed the key, not just the
    row."""
    priv = ed25519.Ed25519PrivateKey.generate()
    km = ThreadKeyManager(MASTER_KEY_HEX, key_store=InMemoryKeyStore())
    saver, serde = _mk_enc_saver(priv, km, InMemoryReceiptLog())

    g = build_graph(saver)
    g.invoke({"messages": ["private"], "count": 0}, {"configurable": {"thread_id": "t-secret"}})

    # Retain a ciphertext copy of some thread state, as a backup/replica/WAL page would.
    tok = current_thread_id.set("t-secret")
    try:
        retained = serde.dumps_typed({"payload": "KEEP_ME"})
        # Non-vacuity: it decrypts while the key is alive.
        assert serde.loads_typed(retained) == {"payload": "KEEP_ME"}
    finally:
        current_thread_id.reset(tok)

    # Crypto-erase the thread.
    saver.delete_thread("t-secret")

    # The retained ciphertext can no longer be decrypted — the key is gone.
    tok = current_thread_id.set("t-secret")
    try:
        with pytest.raises(ThreadKeyDestroyed):
            serde.loads_typed(retained)
    finally:
        current_thread_id.reset(tok)


def test_receipt_commits_to_key_destruction():
    priv = ed25519.Ed25519PrivateKey.generate()
    store = InMemoryKeyStore()
    km = ThreadKeyManager(MASTER_KEY_HEX, key_store=store)
    saver, _ = _mk_enc_saver(priv, km, InMemoryReceiptLog())

    g = build_graph(saver)
    g.invoke({"messages": ["x"], "count": 0}, {"configurable": {"thread_id": "t-k"}})

    # fingerprint recorded for the live key, before destruction
    fp_before = km.fingerprint("t-k")
    assert fp_before and fp_before != "none"

    saver.delete_thread("t-k")
    rcpt = saver.last_receipt("t-k")
    assert rcpt.after == f"key_destroyed:{fp_before}"
    assert rcpt.before != "thread_data" and len(rcpt.before) == 64
    assert rcpt.verify(priv.public_key()) is True
    assert rcpt.verify(ed25519.Ed25519PrivateKey.generate().public_key()) is False
    # key is tombstoned: re-access raises
    with pytest.raises(ThreadKeyDestroyed):
        km.get_key("t-k")


def test_plaintext_checkpoints_still_load_under_key_manager():
    """A v1.1.0 plaintext checkpoint loads under a v1.2.0 key-manager serde (meta has no
    enc flag -> plaintext path)."""
    priv = ed25519.Ed25519PrivateKey.generate()
    plain = GrafomemSerializer(private_key=priv)                      # wrote it (no encryption)
    km = ThreadKeyManager(MASTER_KEY_HEX, key_store=InMemoryKeyStore())
    enc = GrafomemSerializer(private_key=priv, key_manager=km)        # reads it

    obj = {"legacy": "data", "n": 7}
    blob = plain.dumps_typed(obj)
    # No thread in scope, but the blob isn't encrypted, so it still loads.
    assert enc.loads_typed(blob) == obj


def test_cross_thread_read_without_thread_id_errors():
    """Reading an encrypted checkpoint with no thread_id in scope raises ThreadContextMissing;
    with the thread in scope it loads (non-vacuous)."""
    priv = ed25519.Ed25519PrivateKey.generate()
    km = ThreadKeyManager(MASTER_KEY_HEX, key_store=InMemoryKeyStore())
    enc = GrafomemSerializer(private_key=priv, key_manager=km)

    tok = current_thread_id.set("t1")
    try:
        gfm = enc.dumps_typed({"a": 1})
    finally:
        current_thread_id.reset(tok)

    # no thread in scope -> clear error, not a silent wrong read
    assert current_thread_id.get() is None
    with pytest.raises(ThreadContextMissing):
        enc.loads_typed(gfm)

    # with the thread in scope -> loads fine
    tok = current_thread_id.set("t1")
    try:
        assert enc.loads_typed(gfm) == {"a": 1}
    finally:
        current_thread_id.reset(tok)


def test_master_key_required_for_key_manager():
    with pytest.raises(ValueError):
        ThreadKeyManager("tooshort", key_store=InMemoryKeyStore())
