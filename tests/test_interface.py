"""Tests for the MemoryBackend Protocol and ConcurrentMemoryBackend extension.

Extracted from interface.py's __main__ smoke block."""

from datetime import datetime, timezone

import pytest

from aml.backends.interface import (
    Capability,
    CapabilityNotSupported,
    ConcurrentGroup,
    ConcurrentMemoryBackend,
    ConcurrentResult,
    ConflictRule,
    IsolationLevel,
    IsolationPolicy,
    Memory,
    MemoryBackend,
    OpKind,
    RetrieveOptions,
    SubmittedTxn,
    TxnOp,
    WriteOptions,
    verify_provenance,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _Trivial:
    """Minimal MemoryBackend: audit-only, linear scan, no vector index."""

    def __init__(self):
        self._store: list[tuple[int, str, WriteOptions]] = []
        self._next = 0

    def capabilities(self):
        return {Capability.AUDIT}

    def write(self, content, opts=WriteOptions()):
        ref = self._next
        self._next += 1
        self._store.append((ref, content, opts))
        return ref

    def retrieve(self, query, opts=RetrieveOptions()):
        if opts.as_of is not None:
            raise CapabilityNotSupported(Capability.BI_TEMPORAL, "retrieve(as_of)")
        if opts.tenant_id is not None:
            raise CapabilityNotSupported(Capability.MULTI_TENANT, "retrieve(tenant_id)")
        budget = opts.budget_tokens or 9999
        out, used = [], 0
        for ref, content, wopts in self._store:
            if wopts.tenant_id is not None:
                raise CapabilityNotSupported(Capability.MULTI_TENANT, "write(tenant_id)")
            if query.lower() in content.lower():
                cost = len(content)
                if used + cost > budget:
                    break
                out.append(Memory(ref=ref, content=content,
                                  written_at=datetime.now(tz=timezone.utc)))
                used += cost
        return out

    def delete(self, ref):
        raise CapabilityNotSupported(Capability.HARD_DELETE, "delete")

    def supersede(self, old_ref, content, opts=WriteOptions()):
        raise CapabilityNotSupported(Capability.SUPERSESSION_CHAIN, "supersede")

    def audit(self):
        return [Memory(ref=r, content=c,
                        written_at=datetime.now(tz=timezone.utc))
                for r, c, _ in self._store]

    def flush(self):
        pass


@pytest.fixture
def trivial_backend():
    return _Trivial()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_protocol_isinstance(trivial_backend):
    assert isinstance(trivial_backend, MemoryBackend)


def test_capability_enum_stability():
    assert len(Capability) == 10
    # StrEnum values must be stable across versions
    assert Capability.AUDIT.value == "audit"
    assert Capability.CONCURRENCY_CONTROL.value == "concurrency_control"


def test_write_retrieve_round_trip(trivial_backend):
    b = trivial_backend
    r1 = b.write("User prefers tea over coffee", WriteOptions())
    r2 = b.write("User lives in Milan", WriteOptions())
    b.flush()
    mems = b.retrieve("user", RetrieveOptions(budget_tokens=9999))
    assert any("tea" in m.content for m in mems)
    assert {m.ref for m in mems} == {r1, r2}


def test_audit_yields_all(trivial_backend):
    b = trivial_backend
    r1 = b.write("fact one", WriteOptions())
    r2 = b.write("fact two", WriteOptions())
    assert {m.ref for m in b.audit()} == {r1, r2}


def test_budget_tokens_enforced(trivial_backend):
    b = trivial_backend
    b.write("User prefers tea over coffee", WriteOptions())
    b.flush()
    tight = b.retrieve("user", RetrieveOptions(budget_tokens=5))
    assert tight == []


def test_capability_guards_fire(trivial_backend):
    b = trivial_backend
    r1 = b.write("x", WriteOptions())

    with pytest.raises(CapabilityNotSupported):
        b.supersede(r1, "y", WriteOptions())
    with pytest.raises(CapabilityNotSupported):
        b.delete(r1)
    with pytest.raises(CapabilityNotSupported):
        b.retrieve("x", RetrieveOptions(as_of=datetime.now(tz=timezone.utc)))
    with pytest.raises(CapabilityNotSupported):
        b.retrieve("x", RetrieveOptions(tenant_id="A"))


def test_verify_provenance_no_signature():
    assert verify_provenance(
        Memory(ref=0, content="x", written_at=datetime.now(tz=timezone.utc)),
        b"\x00" * 16,
    ) is False


def test_concurrent_memory_backend_extension():
    """ConcurrentMemoryBackend Protocol is implementable and discriminates."""

    class _ConcurrentToy(_Trivial):
        declared_policy = IsolationPolicy(
            level=IsolationLevel.READ_COMMITTED,
            conflict_rule=ConflictRule.LAST_COMMITTER_WINS,
            coverage_guarantee=frozenset(),
        )
        def capabilities(self):
            return {Capability.AUDIT, Capability.CONCURRENCY_CONTROL}
        def submit_concurrent(self, group, policy):
            return ConcurrentResult(committed=[t.txn_id for t in group.transactions])

    cc = _ConcurrentToy()
    b = _Trivial()

    assert isinstance(cc, MemoryBackend)
    assert isinstance(cc, ConcurrentMemoryBackend)
    assert not isinstance(b, ConcurrentMemoryBackend)
    assert Capability.CONCURRENCY_CONTROL in cc.capabilities()
    assert isinstance(cc.declared_policy, IsolationPolicy)

    grp = ConcurrentGroup(
        subject="user_9", predicate="prefers",
        transactions=[
            SubmittedTxn(txn_id="T1", ops=[TxnOp(OpKind.WRITE, content="tea",
                                                  write_options=WriteOptions())]),
            SubmittedTxn(txn_id="T2", ops=[TxnOp(OpKind.WRITE, content="coffee",
                                                  write_options=WriteOptions())],
                         depends_on=["T1"]),
        ],
    )
    res = cc.submit_concurrent(grp, cc.declared_policy)
    assert isinstance(res, ConcurrentResult)
    assert res.committed == ["T1", "T2"]
