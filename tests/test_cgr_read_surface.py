"""Ticket 2 — CGR READ surface: pure unit tests (no DB).

Covers the acceptance-critical properties of the envelope builder and helpers:
honest-scope (score inseparable from evidence + freshness), BOTH evidence masses
(pooled + domain), the SIGNED scope fields and their tamper-resistance, freshness,
and advisory continuity. Route-level no-evidence / domain-match / auth-boundary
behaviour is exercised by the DB route tests (CI `test` job).
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

import aml.cgr.attestation as _attmod
from aml.cgr.attestation import (
    CGR_ATTESTATION_SCHEMA,
    CGR_ATTESTATION_SCHEMA_V4,
    verify_attestation,
)
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
    # the live mint emits the CURRENT default schema — tracked via the constant so the emission
    # bump is a single edit (it is "cgr.attestation.v3" today; the immutable v3 WIRE is locked by
    # the golden fixture test, not here).
    assert env["attestation"]["schema"] == CGR_ATTESTATION_SCHEMA


# ── v4 emission (DORMANT until the constant flips; here exercised via monkeypatch) ──

def _flip_to_v4(monkeypatch):
    """Simulate the emission-bump flip. Patches EXACTLY ONE thing — the single source constant
    aml.cgr.attestation.CGR_ATTESTATION_SCHEMA — proving the flip is that constant and nothing
    else: build_attestation reads it at call time (the mint), and routes.py references it LIVE
    (att['schema'] echo + the advertise endpoints), so one edit propagates everywhere."""
    monkeypatch.setattr(_attmod, "CGR_ATTESTATION_SCHEMA", CGR_ATTESTATION_SCHEMA_V4)


def test_read_surface_emits_v4_body_when_flipped(monkeypatch):
    """With the schema flipped, the read surface emits the pooled-aggregate v4 body:
    ADDS recorded_at + verifiability_tag=judgment; KEEPS agent_handle (cloud-v2 join key),
    scoring_scope=pooled (honest-scope), as_of, last_resolved_at, subject_key; OMITS the
    per-record fields domain/decision_date/backfilled; carries no relates_to; verifies green."""
    _flip_to_v4(monkeypatch)
    env, verify = _env()
    att = env["attestation"]
    assert att["schema"] == "cgr.attestation.v4"
    assert att["recorded_at"] and att["verifiability_tag"] == "judgment"       # v4 ADDS
    assert att["agent_handle"] == "cc-builder@ulissy"                          # cloud-v2 join key survives
    assert att["scoring_scope"] == "pooled" and att["as_of"] and att["last_resolved_at"]
    assert att["subject_key"]
    for f in ("domain", "decision_date", "backfilled", "relates_to"):          # pooled-aggregate absence gate
        assert f not in att
    assert verify_attestation(att, verify) is True                            # signature over the v4 body holds
    # unsigned envelope echoes the v4 convenience copies (authoritative copies signed in att)
    assert env["recorded_at"] == att["recorded_at"] and env["verifiability_tag"] == "judgment"
    assert env["issuer"]["schema"] == "cgr.attestation.v4"


def test_read_surface_v4_round_trips_through_reference_verifier(monkeypatch):
    """PROOF the mint and the verifier agree before anything is served: the read surface's v4
    body verifies GREEN under the reference verifier (@gns-foundation/cgr-verify), pooled-aggregate
    gate satisfied. Cross-language (Python mint -> JS verify) so JCS canonicalization also agrees.
    Skips where node / the verifier harness is unavailable (the minimal CI python job)."""
    _flip_to_v4(monkeypatch)
    verifier = pathlib.Path(__file__).resolve().parent.parent / "clients" / "cgr-verify"
    harness = verifier / "bin" / "verify-v4.mjs"
    # need node AND the verifier's installed deps (@noble/*, canonicalize) — absent in the minimal
    # CI python job (no `npm install`), present in a dev checkout. Skip cleanly rather than fail.
    if shutil.which("node") is None or not harness.exists() or not (verifier / "node_modules").exists():
        pytest.skip("node or clients/cgr-verify deps (node_modules) unavailable")
    env, _ = _env()
    att = env["attestation"]
    payload = json.dumps({
        "subject": att, "ledger": {}, "pinned_issuer": att["issuer_key_id"],
        "held_edges": [], "mode": "non-enforcing", "seek_fails": False,
    })
    proc = subprocess.run(["node", str(harness)], input=payload, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout)
    assert res.get("valid") is True, f"reference verifier rejected the read-surface mint: {res}"
    assert res.get("schema") == "cgr.attestation.v4"


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

def test_issuance_path_emits_null_domain_scope_fields():
    """The ISSUANCE path (/v1/cgr/attestation(s), TierGate, 8b dashboard) uses to_tiergate
    WITHOUT a domain override. It must emit the SAME key set as the read path — the three
    scope fields are always PRESENT, with the domain fields NULL (never absent). This is the
    shape a v3-accepting consumer must tolerate; the keys never vary, only their values."""
    from aml.cgr.engine import to_tiergate
    tg = to_tiergate(_result())
    assert tg["scoring_scope"] == "pooled"
    assert tg["requested_domain"] is None
    assert tg["domain_n_resolved"] is None
    # identical key set to a read-path attestation body (minus the build_attestation-added keys:
    # envelope keys, schema/issuer/issuer_key_id, and — once flipped — the v4 recorded_at +
    # verifiability_tag, which come from build_attestation, not to_tiergate).
    read_att, _ = _env(requested_domain="deploy-verification", domain_n_resolved=2)
    read_body_keys = set(read_att["attestation"]) - {
        "signature", "evidence_ref", "schema", "issuer", "issuer_key_id",
        "recorded_at", "verifiability_tag",
    }
    assert set(tg) == read_body_keys


def test_continuity_states():
    k = "97" * 32
    assert _read_continuity(k, did_key(k))["status"] == "verified"     # no rotation
    assert _read_continuity(k, "did:key:z6MkOther")["status"] == "asserted"  # rotated
    assert _read_continuity(None, None)["status"] == "unverified"      # unbound
    # always advisory — the consumer re-verifies
    assert _read_continuity(k, did_key(k))["advisory"] is True
