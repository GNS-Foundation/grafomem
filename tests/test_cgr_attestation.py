"""CGR Ticket #4a — Foundation issuance-seam tests.

Two layers, matching #1–#3 style:

  Pure (no DB): the neutrality invariant (Foundation key ≠ commercial key, never a
  fallback), build→verify round-trip, tamper/wrong-key rejection, determinism,
  the unproven cold-start attestation, and the fingerprint helper.

  DB/route (local Postgres, same fake-Request harness as test_cgr_substrate):
  GET /v1/cgr/issuer, a verifiable attestation over real Ticket-#1 substrate, the
  503 when FOUNDATION_SIGNING_SEED is unset, and the gcrumbs fingerprint anchor.
"""
from __future__ import annotations

import json
import pathlib
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aml.cgr.attestation import (
    CGR_ATTESTATION_SCHEMA,
    attestation_fingerprint,
    build_attestation,
    canonical_body,
    verify_attestation,
)
from aml.cgr.issuance import (
    FOUNDATION_SEED_ENV,
    FoundationIdentity,
    ISSUER,
    issuer_key_id,
    load_foundation_identity,
    make_signer,
    make_verifier,
)

TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"

# Distinct 32-byte seeds (64 hex chars each).
FOUNDATION_SEED = "11" * 32
COMMERCIAL_SEED = "22" * 32


def _tiergate(handle="invoice-certifier@kapwork-receivables", band="unproven", score=0.5, n=0):
    return {
        "agent_handle": handle,
        "dimension": "receivables",
        "tier": band,
        "cgr_score": score,
        "confidence": 2.0,
        "n_resolved": n,
        "capability_tier": None,
        "as_of": "2026-01-01T00:00:00Z",
        "rationale": "test",
    }


def _signer_verify(seed_hex):
    ident = FoundationIdentity(bytes.fromhex(seed_hex))
    return ident, make_signer(ident), make_verifier(ident.public_key())


# ============================================================================
# Neutrality invariant — the whole point of the ticket
# ============================================================================

def test_foundation_key_is_distinct_from_commercial(monkeypatch):
    """foundation_pubkey != commercial signing_identity.public_key(): the Foundation
    issuer key is a separate key, so reputation is not signed by the key that signs
    the agent's own decisions ("your own credit bureau" at the key level)."""
    monkeypatch.setenv("UNSAFE_LOCAL_DEV", "true")
    monkeypatch.setenv("GRAFOMEM_SIGNING_KEY", COMMERCIAL_SEED)
    monkeypatch.setenv(FOUNDATION_SEED_ENV, FOUNDATION_SEED)

    from aml.cloud.identity import EnvIdentity
    commercial = EnvIdentity()
    foundation = load_foundation_identity()

    assert foundation is not None
    assert foundation.public_key() != commercial.public_key()


def test_missing_seed_returns_none_never_commercial_fallback(monkeypatch):
    """No FOUNDATION_SIGNING_SEED ⇒ None (endpoints 503), even if the commercial
    signing key is present. NEVER falls back to the commercial key."""
    monkeypatch.delenv(FOUNDATION_SEED_ENV, raising=False)
    monkeypatch.setenv("GRAFOMEM_SIGNING_KEY", COMMERCIAL_SEED)
    assert load_foundation_identity() is None


def test_malformed_seed_returns_none(monkeypatch):
    monkeypatch.setenv(FOUNDATION_SEED_ENV, "not-hex")
    assert load_foundation_identity() is None
    monkeypatch.setenv(FOUNDATION_SEED_ENV, "aa" * 16)   # 16 bytes, wrong length
    assert load_foundation_identity() is None


# ============================================================================
# build_attestation / verify_attestation — pure
# ============================================================================

def test_build_verify_roundtrip():
    ident, signer, verify = _signer_verify(FOUNDATION_SEED)
    att = build_attestation(_tiergate(), signer=signer, issuer_key_id=issuer_key_id(ident))
    assert att["schema"] == CGR_ATTESTATION_SCHEMA
    assert att["issuer"] == ISSUER
    assert att["issuer_key_id"] == ident.public_key().hex()
    assert att["evidence_ref"] is None
    assert verify_attestation(att, verify) is True


@pytest.mark.parametrize("field,value", [
    ("cgr_score", 0.99), ("tier", "gold"), ("agent_handle", "attacker@evil"),
    ("n_resolved", 999), ("issuer_key_id", "00" * 32),
])
def test_tamper_any_body_field_fails(field, value):
    ident, signer, verify = _signer_verify(FOUNDATION_SEED)
    att = build_attestation(_tiergate(), signer=signer, issuer_key_id=issuer_key_id(ident))
    att[field] = value                       # tamper after signing
    assert verify_attestation(att, verify) is False


def test_wrong_key_fails():
    ident, signer, _ = _signer_verify(FOUNDATION_SEED)
    att = build_attestation(_tiergate(), signer=signer, issuer_key_id=issuer_key_id(ident))
    # Verify against the COMMERCIAL key ⇒ must fail (neutrality at verify time too).
    commercial = FoundationIdentity(bytes.fromhex(COMMERCIAL_SEED))
    assert verify_attestation(att, make_verifier(commercial.public_key())) is False


def test_determinism_identical_bytes_and_signature():
    ident, signer, _ = _signer_verify(FOUNDATION_SEED)
    a = build_attestation(_tiergate(), signer=signer, issuer_key_id=issuer_key_id(ident))
    b = build_attestation(_tiergate(), signer=signer, issuer_key_id=issuer_key_id(ident))
    assert canonical_body(a) == canonical_body(b)
    assert a["signature"] == b["signature"]           # Ed25519 is deterministic


def test_evidence_ref_is_outside_the_signature():
    """evidence_ref is a post-hoc audit pointer; changing it must NOT break the
    signature (it is written after signing, so cannot be part of what is signed)."""
    ident, signer, verify = _signer_verify(FOUNDATION_SEED)
    a = build_attestation(_tiergate(), signer=signer, issuer_key_id=issuer_key_id(ident))
    b = build_attestation(_tiergate(), signer=signer, issuer_key_id=issuer_key_id(ident),
                          evidence_ref="bc-xyz")
    assert a["signature"] == b["signature"]
    assert verify_attestation(b, verify) is True
    assert attestation_fingerprint(a) == attestation_fingerprint(b)


def test_unproven_agent_gets_valid_signed_attestation():
    ident, signer, verify = _signer_verify(FOUNDATION_SEED)
    att = build_attestation(_tiergate(band="unproven", score=0.5, n=0),
                            signer=signer, issuer_key_id=issuer_key_id(ident))
    assert att["tier"] == "unproven"
    assert verify_attestation(att, verify) is True     # honest cold-start, not an error


def test_fingerprint_changes_on_tamper():
    ident, signer, _ = _signer_verify(FOUNDATION_SEED)
    att = build_attestation(_tiergate(), signer=signer, issuer_key_id=issuer_key_id(ident))
    fp = attestation_fingerprint(att)
    tampered = dict(att, cgr_score=0.99)
    assert attestation_fingerprint(tampered) != fp


# ============================================================================
# JCS (RFC 8785) golden fixture — the cross-language wire-format contract (#4a.1)
# ============================================================================

_GOLDEN = pathlib.Path(__file__).resolve().parent / "fixtures" / "cgr_attestation_v3_jcs.golden.json"
_GOLDEN_V2 = pathlib.Path(__file__).resolve().parent / "fixtures" / "cgr_attestation_v2_jcs.golden.json"
_TIERGATE_KEYS = ("agent_handle", "subject_key", "subject_did", "dimension", "tier", "cgr_score",
                  "confidence", "n_resolved", "capability_tier", "as_of", "last_resolved_at",
                  "scoring_scope", "requested_domain", "domain_n_resolved", "rationale")


def test_binding_invariant_subject_key_distinct():
    """The v2 binding invariant: the bound subject_key (agent GEIANT identity) is
    NEITHER the Foundation issuer key (neutrality) NOR the commercial signing key.
    This is the whole point — reputation is signed by a neutral key, ABOUT an agent
    key, and neither of those may be the agent's own commercial signer."""
    from aml.cgr.identity import did_key
    fx = json.loads(_GOLDEN.read_text())
    commercial = FoundationIdentity(bytes.fromhex(COMMERCIAL_SEED)).public_key().hex()
    assert fx["subject_key"] != fx["issuer_key_id"]                       # ≠ neutrality key
    assert fx["subject_key"] != commercial                               # ≠ commercial signer
    # #7: the identity ANCHOR (subject_did) is likewise neither the issuer nor the commercial key.
    assert fx["subject_did"] != did_key(fx["issuer_key_id"])
    assert fx["subject_did"] != did_key(commercial)
    assert fx["attestation"]["subject_key"] == fx["subject_key"]
    assert fx["attestation"]["subject_did"] == fx["subject_did"]


def test_jcs_golden_fixture_wire_format_locked():
    """The committed JCS canonical bytes are the v3 contract external consumers mirror.
    Locks: exact canonical byte string (incl. subject_key AND last_resolved_at inside
    the signed body), signature verifies over raw bytes under the pinned Foundation
    key, one-byte tamper fails, and signing is deterministic."""
    fx = json.loads(_GOLDEN.read_text())
    att = fx["attestation"]

    # 1. wire format locked — JCS canonical bytes equal the committed string
    assert canonical_body(att).decode("utf-8") == fx["canonical_body_utf8"]
    # v3: subject_key (current op key), subject_did (identity anchor, #7), AND
    # last_resolved_at (freshness, #2) are all INSIDE the signed body.
    assert att["schema"] == "cgr.attestation.v3"
    assert f'"subject_key":"{fx["subject_key"]}"' in fx["canonical_body_utf8"]
    assert f'"subject_did":"{fx["subject_did"]}"' in fx["canonical_body_utf8"]
    assert fx["subject_did"].startswith("did:key:z")
    # #2 (v3): freshness is signed — last_resolved_at is inside the canonical body.
    assert '"last_resolved_at":"' in fx["canonical_body_utf8"]
    assert att["last_resolved_at"]  # present, non-null in the golden
    # Ticket 2: scoring-scope markers are SIGNED — a stripped envelope can't fake domain-scoping.
    assert '"scoring_scope":"pooled"' in fx["canonical_body_utf8"]
    assert '"requested_domain":' in fx["canonical_body_utf8"]
    assert '"domain_n_resolved":' in fx["canonical_body_utf8"]
    # JCS number formatting: integer-valued float serialized WITHOUT ".0"
    assert '"confidence":6,' in fx["canonical_body_utf8"]
    # JCS strings are raw UTF-8, not \uXXXX ascii-escaped
    assert "≥" in fx["canonical_body_utf8"] and "\\u2265" not in fx["canonical_body_utf8"]

    # 2. signature verifies over the raw JCS bytes under the pinned Foundation key
    verify = make_verifier(bytes.fromhex(fx["issuer_key_id"]))
    assert verify_attestation(att, verify) is True

    # 3. one-byte tamper of subject_key / score / freshness / scope fields → signature fails.
    #    The scope-field tampers are the load-bearing ones: a MITM must not be able to flip a
    #    pooled score into a domain-specific claim by editing the (signed) scope markers.
    assert verify_attestation({**att, "subject_key": "00" * 32}, verify) is False
    assert verify_attestation({**att, "cgr_score": 0.99}, verify) is False
    assert verify_attestation({**att, "last_resolved_at": "2020-01-01T00:00:00Z"}, verify) is False
    assert verify_attestation({**att, "scoring_scope": "domain-specific"}, verify) is False
    assert verify_attestation({**att, "requested_domain": "security-scan"}, verify) is False
    assert verify_attestation({**att, "domain_n_resolved": 999}, verify) is False

    # 4. determinism — re-signing the same seed reproduces the exact signature.
    # The seed is the module test constant (NOT stored in the fixture — secret-shaped
    # test material is kept out of committed JSON per the repo's secret-scan guidance).
    ident = FoundationIdentity(bytes.fromhex(FOUNDATION_SEED))
    tiergate = {k: att[k] for k in _TIERGATE_KEYS}
    resigned = build_attestation(tiergate, signer=make_signer(ident), issuer_key_id=issuer_key_id(ident))
    assert resigned["signature"] == att["signature"]


def test_legacy_v2_attestation_still_verifies():
    """Backward-compat: an existing v2 attestation (no last_resolved_at) still verifies
    under the pinned key — verify_attestation is schema-agnostic (re-canon + Ed25519),
    so v1/v2/v3 all verify. Only the freshness field differs across versions."""
    fx = json.loads(_GOLDEN_V2.read_text())
    att = fx["attestation"]
    assert att["schema"] == "cgr.attestation.v2"
    assert "last_resolved_at" not in att
    verify = make_verifier(bytes.fromhex(fx["issuer_key_id"]))
    assert verify_attestation(att, verify) is True
    assert verify_attestation({**att, "cgr_score": 0.99}, verify) is False


# ============================================================================
# DB / route layer — same fake-Request harness as test_cgr_substrate
# ============================================================================

pytestmark_asyncio = pytest.mark.asyncio


def _req(tenant_id: str, scopes=("*",)):
    return SimpleNamespace(state=SimpleNamespace(
        tenant=SimpleNamespace(tenant_id=tenant_id, scopes=list(scopes))))


def _tenant():
    return f"cgr-{uuid.uuid4().hex[:8]}"


def _endpoint(router, path, method="GET"):
    for r in router.routes:
        if r.path == path and method in r.methods:
            return r.endpoint
    raise KeyError(f"{method} {path} not on router")


class _FakeGcrumbs:
    """Records append_breadcrumb calls; returns a stable breadcrumb_id."""
    def __init__(self):
        self.calls = []

    def append_breadcrumb(self, tenant_id, event_type, payload, **kw):
        self.calls.append((tenant_id, event_type, payload))
        return {"breadcrumb_id": "bc-1", "seq": len(self.calls)}


@pytest.fixture(scope="module")
def harness():
    from aml.backends.postgres_gmp import PostgresGMPBackend
    from aml.cloud.decision_trail import DecisionTrailService
    from aml.cloud.execution_receipts import ExecutionReceiptService
    from aml.cloud.demo_routes import (
        GovernedDecisionRequest, OutcomeEvent, create_governed_router,
    )
    from aml.cgr.routes import create_cgr_issuance_router
    from aml.server.stores import StoreManager

    # Commercial signing identity (distinct seed from the Foundation identity).
    class _MockId:
        k = bytes.fromhex(COMMERCIAL_SEED)
        def _priv(self):
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            return Ed25519PrivateKey.from_private_bytes(self.k)
        def sign(self, m):
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
            p = self._priv()
            return p.sign(m), p.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        def public_key(self):
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
            return self._priv().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    dt = DecisionTrailService(TEST_DB_URL)
    dt.ensure_schema()
    receipts = ExecutionReceiptService(TEST_DB_URL, signing_identity=_MockId())
    receipts.ensure_schema()
    store_mgr = StoreManager(lambda: PostgresGMPBackend(TEST_DB_URL))
    gov = create_governed_router(dt, receipts, _MockId(), store_mgr)

    foundation = FoundationIdentity(bytes.fromhex(FOUNDATION_SEED))
    gcrumbs = _FakeGcrumbs()
    iss = create_cgr_issuance_router(dt, store_mgr, foundation, gcrumbs=gcrumbs)
    iss_none = create_cgr_issuance_router(dt, store_mgr, None, gcrumbs=None)

    return SimpleNamespace(
        dt=dt, store_mgr=store_mgr, foundation=foundation, commercial=_MockId(),
        gcrumbs=gcrumbs,
        governed_decision=_endpoint(gov, "/v1/governed/decisions", "POST"),
        post_outcome=_endpoint(gov, "/v1/governed/outcomes", "POST"),
        get_issuer=_endpoint(iss, "/v1/cgr/issuer"),
        get_attestation=_endpoint(iss, "/v1/cgr/attestation/{agent_handle:path}"),
        list_attestations=_endpoint(iss, "/v1/cgr/attestations"),
        get_issuer_none=_endpoint(iss_none, "/v1/cgr/issuer"),
        get_attestation_none=_endpoint(iss_none, "/v1/cgr/attestation/{agent_handle:path}"),
        GovernedDecisionRequest=GovernedDecisionRequest,
        OutcomeEvent=OutcomeEvent,
    )


CERTIFIER = "invoice-certifier@kapwork-receivables"


async def _seed_agent(h, tenant, inv="INV-1", outcome="paid"):
    await h.governed_decision(
        h.GovernedDecisionRequest(decision="certify", reason="ok", invoice_id=inv), _req(tenant))
    if outcome:
        await h.post_outcome(h.OutcomeEvent(invoice_ref=inv, outcome=outcome), _req(tenant))


@pytest.mark.asyncio
async def test_issuer_endpoint_returns_foundation_pubkey(harness):
    out = await harness.get_issuer(_req(_tenant()))
    assert out["issuer"] == ISSUER
    assert out["schema"] == CGR_ATTESTATION_SCHEMA
    assert out["public_key"] == harness.foundation.public_key().hex()
    assert out["issuer_key_id"] == harness.foundation.public_key().hex()
    # The published issuer key is NOT the commercial signing key.
    assert out["public_key"] != harness.commercial.public_key().hex()


@pytest.mark.asyncio
async def test_attestation_endpoint_verifiable_over_substrate(harness):
    T = _tenant()
    await _seed_agent(harness, T)
    att = await harness.get_attestation(CERTIFIER, _req(T))
    assert att["agent_handle"] == CERTIFIER
    assert att["schema"] == CGR_ATTESTATION_SCHEMA
    verify = make_verifier(harness.foundation.public_key())
    assert verify_attestation(att, verify) is True
    # Not verifiable under the commercial key.
    assert verify_attestation(att, make_verifier(harness.commercial.public_key())) is False


@pytest.mark.asyncio
async def test_fingerprint_anchored_in_breadcrumb_when_gcrumbs_present(harness):
    T = _tenant()
    await _seed_agent(harness, T, inv="INV-2")
    n_before = len(harness.gcrumbs.calls)
    att = await harness.get_attestation(CERTIFIER, _req(T))

    assert len(harness.gcrumbs.calls) == n_before + 1
    tenant_id, event_type, payload = harness.gcrumbs.calls[-1]
    assert tenant_id == T
    assert event_type == "cgr:attestation:issued"
    # The breadcrumb carries the fingerprint of exactly what was issued.
    assert payload["attestation_fingerprint"] == attestation_fingerprint(att)
    assert payload["signature"] == att["signature"]
    assert att["evidence_ref"] == "bc-1"


@pytest.mark.asyncio
async def test_missing_seed_endpoints_503(harness):
    T = _tenant()
    with pytest.raises(HTTPException) as e1:
        await harness.get_issuer_none(_req(T))
    assert e1.value.status_code == 503
    with pytest.raises(HTTPException) as e2:
        await harness.get_attestation_none(CERTIFIER, _req(T))
    assert e2.value.status_code == 503


@pytest.mark.asyncio
async def test_unproven_agent_over_substrate_is_valid(harness):
    T = _tenant()
    # A certify with NO outcome ⇒ n_resolved=0 ⇒ band unproven, still signed + valid.
    await _seed_agent(harness, T, inv="INV-3", outcome=None)
    att = await harness.get_attestation(CERTIFIER, _req(T))
    assert att["tier"] == "unproven"
    assert verify_attestation(att, make_verifier(harness.foundation.public_key())) is True
