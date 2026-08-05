"""CGR regression — the FULL governed decision→outcome→score flow on the Postgres
backend WITH content encryption ENABLED (the prod config that was never tested).

Guards the bug where encrypted metadata (cgr_schema/subject/object) was dropped on
read: on Postgres the `metadata` JSONB "{}" sentinel comes back as a DICT (psycopg
auto-parses it), so `_row_to_memory`'s `isinstance(metadata, str)` gate skipped the
`metadata_enc` decrypt — leaving m.metadata = {}. `_tenant_outcomes`/`_tenant_reviews`
then dropped every row on the cgr_schema filter, so outcomes/reviews were invisible to
the scorer (n_resolved stuck at 0, posterior stuck at the capability prior, and the
pre-write idempotency read never saw the just-committed write → "idempotent": false
forever). sqlite (TEXT→str) and unencrypted Postgres (real JSON→dict) both worked, so
the existing tests missed it.

Runs on the default owner/bypass-equivalent test role (RLS inert) — mirroring prod;
the bug is encryption-driven and role-independent, so this reproduces it faithfully.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from aml.backends.postgres_gmp import PostgresGMPBackend
from aml.cloud.tenant_key_manager import FernetEncryptor
from aml.server.stores import StoreManager

TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"


class _MockId:
    """Ed25519 signing identity for execution receipts (mirrors test_cgr_scoring)."""
    def __init__(self):
        self.k = uuid.uuid4().bytes + uuid.uuid4().bytes

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


def _req(tenant_id):
    return SimpleNamespace(state=SimpleNamespace(
        tenant=SimpleNamespace(tenant_id=tenant_id, scopes=["*"])))


def _ep(router, needle, method):
    for r in router.routes:
        if needle in r.path and method in r.methods:
            return r.endpoint
    raise KeyError(f"{method} …{needle} not found")


@pytest.fixture
def enc_app():
    from aml.cloud.decision_trail import DecisionTrailService
    from aml.cloud.execution_receipts import ExecutionReceiptService
    from aml.cloud.demo_routes import (
        GovernedDecisionRequest, OutcomeEvent, create_governed_router,
    )
    from aml.cgr.routes import create_cgr_scoring_router

    enc = FernetEncryptor(Fernet(Fernet.generate_key()))     # prod: content encryption ENABLED
    ident = _MockId()
    dt = DecisionTrailService(TEST_DB_URL); dt.ensure_schema()
    receipts = ExecutionReceiptService(TEST_DB_URL, signing_identity=ident); receipts.ensure_schema()
    sm = StoreManager(lambda: PostgresGMPBackend(TEST_DB_URL, encryption=enc))   # ENCRYPTED store
    gov = create_governed_router(dt, receipts, ident, sm)
    sco = create_cgr_scoring_router(dt, sm)
    app = SimpleNamespace(
        GDR=GovernedDecisionRequest, OutcomeEvent=OutcomeEvent,
        decide=_ep(gov, "/v1/governed/decisions", "POST"),
        outcome=_ep(gov, "/v1/governed/outcomes", "POST"),
        scores=_ep(sco, "/v1/cgr/scores", "GET"),
        sm=sm,
    )
    try:
        yield app
    finally:
        for entry in list(getattr(sm, "_stores", {}).values()):
            try:
                entry.backend.close()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_governed_outcome_resolves_under_postgres_encryption(enc_app):
    T = "enc-" + uuid.uuid4().hex[:8]
    A = f"certifier@{T}"
    INV = f"{T}-INV1"

    # 1. governed certify (judgment) decision — the certification whose outcome we score
    await enc_app.decide(enc_app.GDR(decision="certify", reason="", invoice_id=INV, agent_handle=A), _req(T))

    # 2. scores BEFORE the outcome — the agent sits at its capability prior (nothing resolved)
    before = await enc_app.scores(_req(T))
    sb = next(s for s in before["scores"] if s["agent_handle"] == A)
    assert sb["n_resolved"] == 0
    prior_conf = sb["confidence"]
    prior_alpha = sb["post_alpha"]

    # 3. post the ground-truth outcome (paid)
    r1 = await enc_app.outcome(enc_app.OutcomeEvent(invoice_ref=INV, outcome="paid"), _req(T))
    assert r1["idempotent"] is False

    # 4. scores AFTER — the encrypted outcome MUST now resolve (this is the bug)
    after = await enc_app.scores(_req(T))
    sa = next(s for s in after["scores"] if s["agent_handle"] == A)
    assert sa["n_resolved"] == 1, "encrypted outcome must be visible to the scorer"
    assert sa["confidence"] == prior_conf + 1.0        # one full-weight observation moved the posterior
    assert sa["post_alpha"] == prior_alpha + 1.0       # paid → α increased (posterior off the prior)
    assert sa["cgr_score"] != sb["cgr_score"]

    # 5. idempotency: re-posting the identical outcome is now recognized as a duplicate,
    #    proving the pre-write read (_record_outcome → _tenant_outcomes) sees the commit.
    r2 = await enc_app.outcome(enc_app.OutcomeEvent(invoice_ref=INV, outcome="paid"), _req(T))
    assert r2["idempotent"] is True, "pre-write idempotency read must see the just-committed outcome"


@pytest.mark.asyncio
async def test_governed_review_visible_under_postgres_encryption(enc_app):
    """Reviews travel the same encrypted-metadata read path — an early-signal review on
    an UNRESOLVED certification must reach the scorer (else review calibration is dead)."""
    T = "enc-" + uuid.uuid4().hex[:8]
    A = f"certifier@{T}"
    INV = f"{T}-INV-R"

    await enc_app.decide(enc_app.GDR(decision="certify", reason="", invoice_id=INV, agent_handle=A), _req(T))
    # a review is posted through the governed decisions intake with reason carrying the rating;
    # the OutcomeEvent path above already exercises the store read, so here we assert the
    # review store is readable by writing one and reading it back through the substrate helper.
    from aml.cgr.substrate import CGR_REVIEW_SCHEMA, _tenant_reviews
    from aml.backends.interface import WriteOptions
    from aml.cloud.demo_routes import CGR_REVIEWS_STORE

    rb = enc_app.sm.get_or_create_named(CGR_REVIEWS_STORE).backend
    meta = {"cgr_schema": CGR_REVIEW_SCHEMA, "predicate": "certification_review",
            "subject": INV, "object": "1", "reviewer_handle": f"rev@{T}"}
    rb.write(f"certification_review | {INV} | 1", WriteOptions(tenant_id=T, metadata=meta))

    reviews = _tenant_reviews(rb, T)
    assert len(reviews) == 1, "encrypted review metadata must survive the read path"
    assert reviews[0].metadata.get("cgr_schema") == CGR_REVIEW_SCHEMA
