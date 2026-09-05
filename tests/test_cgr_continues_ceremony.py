"""§5.3 continues-edge ceremony + store loader — unit tests (no DB, no network).

Covers the four precondition checks (control of B — bound to the ceremony; A-revoked via the Supabase
REST API; anti-fork; tier), that run_ceremony REFUSES and writes nothing when any fails, and that
load_continues_edges round-trips the record shape continues_edge_metadata produces.
"""
from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import aml.cgr.substrate as substrate
from aml.cgr.substrate import CGR_CONTINUES_SCHEMA, continues_edge_metadata

# import the one-shot ceremony script by path (scripts/ is not a package)
_CEREMONY_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "cgr_continues_ceremony.py"
_spec = importlib.util.spec_from_file_location("cgr_continues_ceremony", _CEREMONY_PATH)
ceremony = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ceremony)

_A_CERT = "96" * 32   # A's cert_hash for these tests


# ── control of B — signature must be over the BOUND ceremony message ────────────────────

def _b_keypair():
    sk = Ed25519PrivateKey.generate()
    pub = sk.public_key().public_bytes_raw().hex()
    return sk, pub


def _sign_bound(sk, b_pub, a_cert, nonce):
    return sk.sign(ceremony.ceremony_message(b_pub, a_cert, nonce).encode()).hex()


def test_control_of_b_bound_signature_passes():
    sk, pub = _b_keypair()
    sig = _sign_bound(sk, pub, _A_CERT, "rand-123")
    ok, _ = ceremony.check_control_of_b(pub, "rand-123", sig, _A_CERT)
    assert ok is True


def test_control_of_b_bare_nonce_signature_refuses():
    """A signature over the BARE nonce (not the bound ceremony message) must NOT pass — this is the
    nonce-binding fix: a stray B-signature can't be replayed into this ceremony."""
    sk, pub = _b_keypair()
    bare = sk.sign(b"rand-123").hex()                        # signed the nonce alone, unbound
    ok, reason = ceremony.check_control_of_b(pub, "rand-123", bare, _A_CERT)
    assert ok is False and "control-of-B" in reason


def test_control_of_b_wrong_cert_binding_refuses():
    """A signature bound to a DIFFERENT A cert must not pass for this A."""
    sk, pub = _b_keypair()
    sig = _sign_bound(sk, pub, "ab" * 32, "rand-123")        # bound to a different cert
    ok, _ = ceremony.check_control_of_b(pub, "rand-123", sig, _A_CERT)
    assert ok is False


def test_control_of_b_wrong_key_refuses():
    sk, pub = _b_keypair()
    other, _ = _b_keypair()
    sig = _sign_bound(other, pub, _A_CERT, "rand-123")       # signed by a different key
    ok, _ = ceremony.check_control_of_b(pub, "rand-123", sig, _A_CERT)
    assert ok is False


# ── A genuinely retired (geiant agent_registry via Supabase REST) ───────────────────────

class _FakeResp:
    def __init__(self, rows, status=200):
        self._rows, self.status_code = rows, status
    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]
    def json(self):
        return self._rows


def _patch_registry(monkeypatch, rows, status=200):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResp(rows, status))


def test_a_revoked_passes_when_revoked_at_set(monkeypatch):
    _patch_registry(monkeypatch, [{"agent_pk": "c14094ea", "revoked_at": "2026-08-31T00:00:00Z"}])
    ok, _ = ceremony.check_a_revoked("https://x.supabase.co", "svc", "c14094ea")
    assert ok is True


def test_a_revoked_refuses_when_live(monkeypatch):
    _patch_registry(monkeypatch, [{"agent_pk": "c14094ea", "revoked_at": None}])
    ok, reason = ceremony.check_a_revoked("https://x.supabase.co", "svc", "c14094ea")
    assert ok is False and "live" in reason.lower()


def test_a_revoked_refuses_when_absent(monkeypatch):
    _patch_registry(monkeypatch, [])                         # zero rows
    ok, reason = ceremony.check_a_revoked("https://x.supabase.co", "svc", "c14094ea")
    assert ok is False and "not found" in reason.lower()


def test_a_revoked_refuses_on_read_error(monkeypatch):
    _patch_registry(monkeypatch, [], status=500)            # never fail-open on a read error
    ok, reason = ceremony.check_a_revoked("https://x.supabase.co", "svc", "c14094ea")
    assert ok is False and "read error" in reason.lower()


# ── anti-fork uniqueness (a check, not a constraint) ────────────────────────────────────

def test_anti_fork_refuses_on_existing_edge_to_same_cert(monkeypatch):
    monkeypatch.setattr(substrate, "load_continues_edges",
                        lambda sm, t: {"someB": {"target": {"hash": _A_CERT}}})
    ok, reason = ceremony.check_anti_fork(object(), "tenant", _A_CERT)
    assert ok is False and "anti-fork" in reason


def test_anti_fork_passes_when_no_edge_targets_a(monkeypatch):
    monkeypatch.setattr(substrate, "load_continues_edges",
                        lambda sm, t: {"otherB": {"target": {"hash": "cd" * 32}}})
    ok, _ = ceremony.check_anti_fork(object(), "tenant", _A_CERT)
    assert ok is True


# ── tier ────────────────────────────────────────────────────────────────────────────────

def test_tier_accepts_closed_vocab():
    assert ceremony.check_tier("operator_verification")[0] is True
    assert ceremony.check_tier("custody_record")[0] is True


def test_tier_refuses_unknown():
    assert ceremony.check_tier("hearsay")[0] is False


# ── run_ceremony refuses & writes nothing when a precondition fails ──────────────────────

class _NoWriteSM:
    def get_or_create_named(self, *a):
        raise AssertionError("must not write")


def _valid_b(nonce="n"):
    sk, pub = _b_keypair()
    return pub, _sign_bound(sk, pub, _A_CERT, nonce), nonce


def test_run_ceremony_refuses_and_writes_nothing_when_a_is_live(monkeypatch):
    pub, sig, nonce = _valid_b()
    monkeypatch.setattr(substrate, "load_continues_edges", lambda sm, t: {})   # anti-fork clean
    _patch_registry(monkeypatch, [{"revoked_at": None}])                        # A live → refuse
    code = ceremony.run_ceremony(
        store_manager=_NoWriteSM(), geiant_supabase_url="https://x.supabase.co",
        geiant_service_role_key="svc", tenant_id="t", b_key=pub, nonce=nonce, b_sig=sig,
        a_agent_pk="c14094ea", a_cert_hash=_A_CERT, decision_date="2026-09-04",
    )
    assert code != 0


def test_run_ceremony_dry_run_passes_all_and_writes_nothing(monkeypatch):
    pub, sig, nonce = _valid_b()
    monkeypatch.setattr(substrate, "load_continues_edges", lambda sm, t: {})
    _patch_registry(monkeypatch, [{"revoked_at": "2026-08-31"}])                # A revoked
    code = ceremony.run_ceremony(
        store_manager=_NoWriteSM(), geiant_supabase_url="https://x.supabase.co",
        geiant_service_role_key="svc", tenant_id="t", b_key=pub, nonce=nonce, b_sig=sig,
        a_agent_pk="c14094ea", a_cert_hash=_A_CERT, decision_date="2026-09-04", dry_run=True,
    )
    assert code == 0


# ── loader round-trips the record shape ─────────────────────────────────────────────────

def test_load_continues_edges_builds_inject_ready_edge(monkeypatch):
    meta = continues_edge_metadata(
        subject_key="d3caa6f1", target_hash=_A_CERT, evidence_tier="operator_verification",
        decision_date="2026-09-04", recorded_at="2026-09-04T00:00:00Z")
    assert meta["cgr_schema"] == CGR_CONTINUES_SCHEMA
    rec = SimpleNamespace(metadata=meta, tenant_id="t")
    other = SimpleNamespace(metadata={"cgr_schema": "cgr.rotation.v1"}, tenant_id="t")
    monkeypatch.setattr(substrate, "_scoped_audit", lambda backend, tid: [rec, other])

    class _SM:
        def get_or_create_named(self, name): return SimpleNamespace(backend=object())

    edges = substrate.load_continues_edges(_SM(), "t")
    assert set(edges) == {"d3caa6f1"}                          # only the continues record
    assert edges["d3caa6f1"] == {
        "type": "continues",
        "target": {"kind": "delegation_cert", "hash_alg": "sha-256", "hash": _A_CERT},
        "evidence_tier": "operator_verification"}
