"""Ticket 2 — CGR READ surface: pure unit tests (no DB).

Covers the acceptance-critical properties of the envelope builder and helpers:
honest-scope (score inseparable from evidence + freshness), BOTH evidence masses
(pooled + domain), the SIGNED scope fields and their tamper-resistance, freshness,
and advisory continuity. Route-level no-evidence / domain-match / auth-boundary
behaviour is exercised by the DB route tests (CI `test` job).
"""
from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric import ed25519

from aml.cgr.attestation import verify_attestation
from aml.cgr.identity import did_key
from aml.cgr.routes import (
    READ_SURFACE_VERSION,
    _read_continuity,
    _read_freshness,
    build_read_envelope,
)
from aml.cgr.scoring import CGRResult


def _keypair():
    sk = ed25519.Ed25519PrivateKey.generate()
    pk = sk.public_key()
    kid = pk.public_bytes_raw().hex()
    def signer(b: bytes) -> str:
        return sk.sign(b).hex()
    def verify(msg: bytes, sig_hex: str) -> bool:
        try:
            pk.verify(bytes.fromhex(sig_hex), msg); return True
        except Exception:
            return False
    return signer, verify, kid


def _result(**over):
    subj = over.pop("subject_key", "97" * 32)
    did = over.pop("subject_did", did_key(subj))
    base = dict(agent_handle="cc-builder@ulissy", cgr_score=0.8, confidence=5.0,
                n_resolved=3, n_pending=0, capability_tier=None,
                as_of="2026-08-23T12:00:00+00:00", dimension="receivables",
                subject_key=subj, subject_did=did, last_resolved_at="2026-08-23T11:59:00+00:00")
    base.update(over)
    return CGRResult(**base)


def _env(requested_domain="deploy-verification", domain_n_resolved=2, **over):
    signer, verify, kid = _keypair()
    env = build_read_envelope(_result(**over), requested_domain, domain_n_resolved,
                              signer=signer, issuer_key_id_hex=kid, issuer_pubkey_hex=kid)
    return env, verify


# ── honest-scope: score is never returned without its evidence + freshness ──────

def test_envelope_shape_and_version():
    env, _ = _env()
    assert env["surface_version"] == READ_SURFACE_VERSION
    assert env["result"] == "attestation"
    assert "attestation" in env and "issuer" in env and "verify" in env


def test_score_is_inseparable_from_evidence_and_freshness():
    env, _ = _env()
    # a non-null score MUST come with both evidence masses and a freshness block —
    # a bare score is structurally unobtainable from this surface.
    assert env["score"] is not None
    assert env["evidence_mass"] is not None          # pooled n
    assert env["n_resolved"] is not None
    assert env["freshness"] and "last_resolved_at" in env["freshness"] and "stale" in env["freshness"]


def test_both_evidence_masses_reported_distinctly():
    env, _ = _env(requested_domain="deploy-verification", domain_n_resolved=2)
    # pooled evidence backs the score; domain evidence backs the domain match — separate.
    assert env["evidence_mass"] == 5.0 and env["n_resolved"] == 3     # pooled (score)
    assert env["domain_n_resolved"] == 2                              # domain match
    assert env["scoring_scope"] == "pooled"
    # and both are also inside the signed body:
    att = env["attestation"]
    assert att["n_resolved"] == 3 and att["domain_n_resolved"] == 2 and att["scoring_scope"] == "pooled"


def test_no_per_domain_score_implied():
    env, _ = _env(requested_domain="deploy-verification")
    # the scoring dimension stays receivables (v0, single-dimension); the requested domain
    # is recorded but the score is NOT relabelled as domain-specific.
    assert env["attestation"]["dimension"] == "receivables"
    assert env["attestation"]["requested_domain"] == "deploy-verification"
    assert env["attestation"]["scoring_scope"] == "pooled"


# ── signature round-trip + tamper (incl. the new SIGNED scope fields) ───────────

def test_attestation_verifies_round_trip():
    env, verify = _env()
    assert verify_attestation(env["attestation"], verify) is True
    assert env["attestation"]["schema"] == "cgr.attestation.v3"


def test_tamper_of_scope_fields_fails_verification():
    env, verify = _env(requested_domain="deploy-verification", domain_n_resolved=2)
    att = env["attestation"]
    # the load-bearing ones: a MITM must not flip a pooled score into a domain-specific claim
    assert verify_attestation({**att, "requested_domain": "security-scan"}, verify) is False
    assert verify_attestation({**att, "domain_n_resolved": 999}, verify) is False
    assert verify_attestation({**att, "scoring_scope": "domain-specific"}, verify) is False
    # and the usual suspects
    assert verify_attestation({**att, "cgr_score": 0.99}, verify) is False
    assert verify_attestation({**att, "last_resolved_at": "2020-01-01T00:00:00Z"}, verify) is False


def test_no_domain_requested_signs_nulls():
    env, verify = _env(requested_domain=None, domain_n_resolved=None)
    att = env["attestation"]
    assert att["requested_domain"] is None and att["domain_n_resolved"] is None
    assert att["scoring_scope"] == "pooled"
    assert verify_attestation(att, verify) is True


# ── freshness ──────────────────────────────────────────────────────────────────

def test_freshness_none_is_stale():
    f = _read_freshness(None)
    assert f["stale"] is True and f["last_resolved_at"] is None and f["age_ms"] is None


def test_freshness_old_is_stale_recent_is_fresh():
    assert _read_freshness("2020-01-01T00:00:00Z")["stale"] is True
    from datetime import datetime, timezone
    recent = datetime.now(timezone.utc).isoformat()
    assert _read_freshness(recent)["stale"] is False


# ── advisory continuity ─────────────────────────────────────────────────────────

def test_continuity_states():
    k = "97" * 32
    assert _read_continuity(k, did_key(k))["status"] == "verified"     # no rotation
    assert _read_continuity(k, "did:key:z6MkOther")["status"] == "asserted"  # rotated
    assert _read_continuity(None, None)["status"] == "unverified"      # unbound
    # always advisory — the consumer re-verifies
    assert _read_continuity(k, did_key(k))["advisory"] is True
