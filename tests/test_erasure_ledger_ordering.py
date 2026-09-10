"""Ledger unavailability must FAIL the erasure request — never issue a certificate.

The incident this pins (2026-09-10, grafomem-internal): the ledger write ran
AFTER the certificate insert, unguarded. With the ledger pool unable to
authenticate, 14 signed certificates were issued with no ledger row — a
certificate asserting an erasure that the restore-time record knows nothing
about. The fix inverts the order: ledger row first, then sign, then persist the
certificate; a dead ledger aborts the request before any certificate exists.

The ledger here is REAL (ErasureLedger against a dead port), not a stub — a stub
that raises proves the stub raises.
"""
import os
import uuid

import psycopg
import pytest

from aml.cloud.erasure_ledger import ErasureLedger
from aml.cloud.erasure_proof import ErasureProofService
from aml.cloud.identity import EnvIdentity

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")

DEAD_LEDGER_URL = "postgresql://nobody@127.0.0.1:9/nowhere"   # port 9 (discard) — connect fails fast


def _cert_count(fact_ref: int) -> int:
    with psycopg.connect(DB_URL, autocommit=True) as c:
        return c.execute(
            "SELECT count(*) FROM erasure_certificates WHERE fact_ref = %s", (fact_ref,)
        ).fetchone()[0]


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setenv("GRAFOMEM_SIGNING_KEY", "01" * 32)
    monkeypatch.setenv("UNSAFE_LOCAL_DEV", "true")
    return EnvIdentity()


@pytest.fixture
def service_with_dead_ledger(monkeypatch, identity):
    monkeypatch.setenv("GRAFOMEM_DB_POOL_TIMEOUT", "2")   # fail fast, not in 10s
    ledger = ErasureLedger(DEAD_LEDGER_URL, open=False)
    ledger._pool.open()   # pool must be open to attempt (and fail) connections
    return ErasureProofService(
        DB_URL,
        signing_identity=identity,
        erasure_ledger=ledger,
    )


@pytest.fixture
def service_with_live_ledger(identity):
    ledger = ErasureLedger(DB_URL, open=True)
    ledger.ensure_schema()
    return ErasureProofService(
        DB_URL,
        signing_identity=identity,
        erasure_ledger=ledger,
    )


def test_dead_ledger_blocks_certificate(service_with_dead_ledger):
    """Pool down -> the request raises AND no certificate row exists."""
    fact_ref = int(uuid.uuid4().int % 2_000_000_000)
    with pytest.raises(Exception) as exc:
        service_with_dead_ledger.issue_certificate(
            tenant_id=f"t-ledger-{uuid.uuid4().hex[:8]}", fact_ref=fact_ref,
            fact_content="to-be-erased",
        )
    assert _cert_count(fact_ref) == 0, (
        "INCIDENT SHAPE REPRODUCED: a certificate row was issued while the "
        "erasure ledger was unavailable — exactly the state that produced 14 "
        "unrecorded erasures in production"
    )
    assert "ledger" in str(exc.value).lower(), (
        f"the failure must NAME the ledger so the caller knows what to fix, got: {exc.value}"
    )


def test_live_ledger_row_written_with_certificate(service_with_live_ledger):
    """Happy path: certificate issued AND the ledger row exists, same certificate_id."""
    fact_ref = int(uuid.uuid4().int % 2_000_000_000)
    tenant = f"t-ledger-{uuid.uuid4().hex[:8]}"
    cert = service_with_live_ledger.issue_certificate(
        tenant_id=tenant, fact_ref=fact_ref, fact_content="to-be-erased",
    )
    assert cert.signature is not None
    with psycopg.connect(DB_URL, autocommit=True) as c:
        n = c.execute(
            "SELECT count(*) FROM erasure_ledger WHERE entry_id = %s AND tenant_id = %s",
            (cert.certificate_id, tenant),
        ).fetchone()[0]
    assert n == 1, "the ledger row must exist and share the certificate's id"


def test_no_ledger_configured_still_issues(identity):
    """Self-hosted shape: no ledger wired -> certificates still issue (unchanged)."""
    svc = ErasureProofService(DB_URL, signing_identity=identity)
    fact_ref = int(uuid.uuid4().int % 2_000_000_000)
    cert = svc.issue_certificate(
        tenant_id=f"t-ledger-{uuid.uuid4().hex[:8]}", fact_ref=fact_ref,
        fact_content="x",
    )
    assert cert.signature is not None
    assert _cert_count(fact_ref) == 1
