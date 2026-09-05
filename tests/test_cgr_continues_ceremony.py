"""§5.3 continues-edge ceremony + store loader — unit tests (no DB, no network).

Covers the four precondition checks (control of B, A-revoked, anti-fork, tier), that run_ceremony
REFUSES and writes nothing when any fails, and that load_continues_edges round-trips the record shape
continues_edge_metadata produces.
"""
from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import aml.cgr.substrate as substrate
from aml.cgr.substrate import CGR_CONTINUES_SCHEMA, continues_edge_metadata

# import the one-shot ceremony script by path (scripts/ is not a package)
_CEREMONY_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cgr_continues_ceremony.py"
_spec = importlib.util.spec_from_file_location("cgr_continues_ceremony", _CEREMONY_PATH)
ceremony = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ceremony)


# ── control of B (Ed25519 nonce) ────────────────────────────────────────────────────────

def _b_keypair():
    sk = Ed25519PrivateKey.generate()
    pub = sk.public_key().public_bytes_raw().hex()
    return sk, pub


def test_control_of_b_valid_signature_passes():
    sk, pub = _b_keypair()
    nonce = "challenge-123"
    sig = sk.sign(nonce.encode()).hex()
    ok, _ = ceremony.check_control_of_b(pub, nonce, sig)
    assert ok is True


def test_control_of_b_wrong_signature_refuses():
    sk, pub = _b_keypair()
    other, _ = _b_keypair()
    nonce = "challenge-123"
    bad = other.sign(nonce.encode()).hex()          # signed by a different key
    ok, reason = ceremony.check_control_of_b(pub, nonce, bad)
    assert ok is False and "control-of-B" in reason


def test_control_of_b_tampered_nonce_refuses():
    sk, pub = _b_keypair()
    sig = sk.sign(b"challenge-123").hex()
    ok, _ = ceremony.check_control_of_b(pub, "challenge-XXX", sig)   # nonce changed after signing
    assert ok is False


# ── A genuinely retired (geiant agent_registry) ─────────────────────────────────────────

class _FakeCursor:
    def __init__(self, row): self._row = row
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **k): pass
    def fetchone(self): return self._row


class _FakeConn:
    def __init__(self, row): self._row = row
    def cursor(self): return _FakeCursor(self._row)


def test_a_revoked_passes_when_revoked_at_set():
    ok, _ = ceremony.check_a_revoked(_FakeConn(("2026-08-31T00:00:00Z",)), "c14094ea")
    assert ok is True


def test_a_revoked_refuses_when_live():
    ok, reason = ceremony.check_a_revoked(_FakeConn((None,)), "c14094ea")   # revoked_at IS NULL
    assert ok is False and "live" in reason.lower()


def test_a_revoked_refuses_when_absent():
    ok, reason = ceremony.check_a_revoked(_FakeConn(None), "c14094ea")      # not in registry
    assert ok is False and "not found" in reason.lower()


# ── anti-fork uniqueness ────────────────────────────────────────────────────────────────

def test_anti_fork_refuses_on_existing_edge_to_same_cert(monkeypatch):
    a_cert = "ab" * 32
    monkeypatch.setattr(substrate, "load_continues_edges",
                        lambda sm, t: {"someB": {"target": {"hash": a_cert}}})
    ok, reason = ceremony.check_anti_fork(object(), "tenant", a_cert)
    assert ok is False and "anti-fork" in reason


def test_anti_fork_passes_when_no_edge_targets_a(monkeypatch):
    monkeypatch.setattr(substrate, "load_continues_edges",
                        lambda sm, t: {"otherB": {"target": {"hash": "cd" * 32}}})
    ok, _ = ceremony.check_anti_fork(object(), "tenant", "ab" * 32)
    assert ok is True


# ── tier ────────────────────────────────────────────────────────────────────────────────

def test_tier_accepts_closed_vocab():
    assert ceremony.check_tier("operator_verification")[0] is True
    assert ceremony.check_tier("custody_record")[0] is True


def test_tier_refuses_unknown():
    assert ceremony.check_tier("hearsay")[0] is False


# ── run_ceremony refuses & writes nothing when a precondition fails ──────────────────────

def test_run_ceremony_refuses_and_writes_nothing_when_a_is_live(monkeypatch):
    sk, pub = _b_keypair()
    nonce = "n"
    sig = sk.sign(nonce.encode()).hex()
    monkeypatch.setattr(substrate, "load_continues_edges", lambda sm, t: {})   # anti-fork clean
    # a "store" that explodes if written to — proves no write happens on refusal
    class _NoWriteSM:
        def get_or_create_named(self, *a): raise AssertionError("must not write on refusal")
    code = ceremony.run_ceremony(
        store_manager=_NoWriteSM(), geiant_conn=_FakeConn((None,)),   # A live → refuse
        tenant_id="t", b_key=pub, nonce=nonce, b_sig=sig,
        a_agent_pk="c14094ea", a_cert_hash="ab" * 32, decision_date="2026-09-04",
    )
    assert code != 0


def test_run_ceremony_dry_run_passes_all_and_writes_nothing(monkeypatch):
    sk, pub = _b_keypair()
    nonce = "n"
    sig = sk.sign(nonce.encode()).hex()
    monkeypatch.setattr(substrate, "load_continues_edges", lambda sm, t: {})
    class _NoWriteSM:
        def get_or_create_named(self, *a): raise AssertionError("dry-run must not write")
    code = ceremony.run_ceremony(
        store_manager=_NoWriteSM(), geiant_conn=_FakeConn(("2026-08-31",)),   # A revoked
        tenant_id="t", b_key=pub, nonce=nonce, b_sig=sig,
        a_agent_pk="c14094ea", a_cert_hash="ab" * 32, decision_date="2026-09-04",
        dry_run=True,
    )
    assert code == 0


# ── loader round-trips the record shape ─────────────────────────────────────────────────

def test_load_continues_edges_builds_inject_ready_edge(monkeypatch):
    meta = continues_edge_metadata(
        subject_key="d3caa6f1", target_hash="ab" * 32, evidence_tier="operator_verification",
        decision_date="2026-09-04", recorded_at="2026-09-04T00:00:00Z")
    assert meta["cgr_schema"] == CGR_CONTINUES_SCHEMA
    rec = SimpleNamespace(metadata=meta, tenant_id="t")
    other = SimpleNamespace(metadata={"cgr_schema": "cgr.rotation.v1"}, tenant_id="t")
    # _scoped_audit is the store read seam; fake it to yield our record + an unrelated one
    monkeypatch.setattr(substrate, "_scoped_audit", lambda backend, tid: [rec, other])
    monkeypatch.setattr(substrate, "_store_backend", lambda sm, name: object(), raising=False)

    class _SM:
        def get_or_create_named(self, name): return SimpleNamespace(backend=object())

    edges = substrate.load_continues_edges(_SM(), "t")
    assert set(edges) == {"d3caa6f1"}                          # only the continues record
    e = edges["d3caa6f1"]
    assert e == {"type": "continues",
                 "target": {"kind": "delegation_cert", "hash_alg": "sha-256", "hash": "ab" * 32},
                 "evidence_tier": "operator_verification"}
