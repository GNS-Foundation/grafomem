"""I0b stage-1 — coverage verification must-fail tests (decision 0014). Hermetic (no DB).

Coverage is a MEASUREMENT, not a default. These pin the stage-1 contract and FAIL on
pre-stage-1 code (which auto-defaults omitted coverage to {"primary":"absent"} and has no
probe / no `backend` parameter / no `probe_coverage` helper) — watch them fail first.

The contract (0014 + operator decisions 2026-09-16):
  * `present`/`absent` are VERIFIED statuses (a real read after the scrub); `unverified` is
    the honest value for an unprobed subsystem and is NOT a claim of absence.
  * A backend that cannot probe (no POINT_LOOKUP capability, or no `exists` method) yields
    `unverified` for primary — never a fabricated `absent`.
  * `decision_trail` is `absent` only from a read-after-write; otherwise `unverified`.
  * Gate 1 is a VACUITY gate: a certificate whose coverage has zero verified entries
    (empty, all-`unverified`, or the removed auto-default) is refused.
"""
import pytest

from aml.backends.interface import Capability
from aml.cloud.erasure_proof import (
    ErasureProofService,
    EmptyGovernanceCoverage,
    probe_coverage,
)


# ── hermetic harness (self-contained; mirrors test_erasure_i0b) ───────────────
class _MockId:
    def __init__(self, k=b"0" * 32):
        self.k = k

    def sign(self, m):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        priv = Ed25519PrivateKey.from_private_bytes(self.k)
        return priv.sign(m), priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def public_key(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return Ed25519PrivateKey.from_private_bytes(self.k).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


class _Result:
    def fetchall(self): return []
    def fetchone(self): return None


class _FakeConn:
    closed = False

    def __init__(self, events):
        self.events = events

    def execute(self, sql, params=None):
        if "INSERT INTO" in sql and "erasure_certificates" in sql:
            self.events.append("cert_insert")
        return _Result()


class _FakeDT:
    def __init__(self): self.scrubbed = []
    def scrub_fact(self, fact_ref, tenant_id, **k): self.scrubbed.append(fact_ref); return 0


class _FakeLedger:
    def __init__(self, events, fail=False): self.events, self.fail, self.rows = events, fail, []
    def record_subject_erasure(self, **kw):
        if self.fail:
            raise RuntimeError("ledger pool down")
        self.events.append("ledger_write")
        self.rows.append(kw)


def _cert_inserted(events): return "cert_insert" in events


# ── fake memory backends ─────────────────────────────────────────────────────
class _ProbeBackend:
    """A backend that CAN answer exists(ref)."""
    def __init__(self, present: bool):
        self._present = present
    def capabilities(self):
        return {Capability.POINT_LOOKUP}
    def exists(self, ref):
        return self._present


class _NoCapBackend:
    """Has an exists() method but does NOT claim POINT_LOOKUP — must not be trusted."""
    def capabilities(self):
        return set()
    def exists(self, ref):  # present, but we must ignore it (no capability)
        return True


class _NoMethodBackend:
    """Claims nothing and has no exists() method at all."""
    def capabilities(self):
        return set()


def _svc(events, ledger, dt="__default__"):
    # Faithful: dt=None means NO decision trail (no read-after-write). Omitted → _FakeDT.
    trail = _FakeDT() if dt == "__default__" else dt
    s = ErasureProofService(db_url=None, decision_trail=trail,
                            signing_identity=_MockId(), erasure_ledger=ledger)
    s._conn = _FakeConn(events)
    return s


# ── probe_coverage unit tests (the honest-status core) ───────────────────────
def test_backend_without_capability_yields_unverified_not_absent():
    cov = probe_coverage(_NoCapBackend(), 123, decision_trail_absent=False)
    assert cov["primary"] == "unverified", "no POINT_LOOKUP → must NOT fabricate absent"


def test_backend_without_exists_method_yields_unverified():
    cov = probe_coverage(_NoMethodBackend(), 123, decision_trail_absent=False)
    assert cov["primary"] == "unverified"


def test_no_backend_yields_unverified():
    cov = probe_coverage(None, 123, decision_trail_absent=False)
    assert cov["primary"] == "unverified"


def test_probe_absent_sets_absent():
    cov = probe_coverage(_ProbeBackend(present=False), 123, decision_trail_absent=False)
    assert cov["primary"] == "absent", "a real read finding the fact gone is a verified absent"


def test_probe_present_sets_present():
    cov = probe_coverage(_ProbeBackend(present=True), 123, decision_trail_absent=False)
    assert cov["primary"] == "present", "a real read finding the fact still there is present"


def test_decision_trail_absent_only_from_read_after_write():
    absent = probe_coverage(None, 123, decision_trail_absent=True)
    present = probe_coverage(None, 123, decision_trail_absent=False)
    assert absent["decision_trail"] == "absent"
    assert present["decision_trail"] == "unverified"


def test_declared_extra_subsystems_land_unverified():
    cov = probe_coverage(None, 123, decision_trail_absent=False, declared_extra=["embedding", "cache"])
    assert cov["embedding"] == "unverified" and cov["cache"] == "unverified"


# ── issue_certificate: the vacuity gate + no auto-default ─────────────────────
def test_auto_default_refused_when_nothing_verified():
    """No backend, no read-after-write (dt=None), no verified entry → refuse. On
    pre-stage-1 code this SUCCEEDS via the {"primary":"absent"} auto-default — the bug."""
    ev = []
    svc = _svc(ev, _FakeLedger(ev), dt=None)
    with pytest.raises(EmptyGovernanceCoverage):
        svc.issue_certificate("t1", 123, backend=None)
    assert not _cert_inserted(ev), "no certificate may be issued with zero verified coverage"


def test_all_unverified_refused():
    ev = []
    svc = _svc(ev, _FakeLedger(ev), dt=None)
    with pytest.raises(EmptyGovernanceCoverage):
        svc.issue_certificate("t1", 123, backend=_NoCapBackend(),
                              declared_subsystems=["embedding", "cache"])
    assert not _cert_inserted(ev), "all-unverified coverage is vacuous → refuse"


def test_backend_without_exists_yields_unverified_not_absent_end_to_end():
    """A non-probing backend must not let a certificate claim absence."""
    ev = []
    svc = _svc(ev, _FakeLedger(ev), dt=None)
    with pytest.raises(EmptyGovernanceCoverage):
        svc.issue_certificate("t1", 123, backend=_NoMethodBackend())
    assert not _cert_inserted(ev)


def test_probe_present_issues_certificate_saying_present():
    """exists→True: the fact survived the delete. present is a verified status, so a
    certificate IS issued, and it must honestly record present (an erasure-incomplete signal)."""
    ev = []
    svc = _svc(ev, _FakeLedger(ev))
    cert = svc.issue_certificate("t1", 123, backend=_ProbeBackend(present=True))
    assert cert is not None
    assert cert.coverage["primary"] == "present", "certificate must record the probed present"


def test_probe_absent_issues_certificate_saying_absent():
    ev = []
    svc = _svc(ev, _FakeLedger(ev))
    cert = svc.issue_certificate("t1", 123, backend=_ProbeBackend(present=False))
    assert cert is not None
    assert cert.coverage["primary"] == "absent", "verified absent, not a default"
    assert _cert_inserted(ev)


# ── server-side probe: the REST route has no backend handle → the service resolves
#    one via backend_resolver (operator decision 2, 2026-09-16) ──────────────────
def test_backend_resolver_probes_server_side():
    ev = []
    s = ErasureProofService(db_url=None, decision_trail=None, signing_identity=_MockId(),
                            erasure_ledger=_FakeLedger(ev),
                            backend_resolver=lambda tid: _ProbeBackend(present=False))
    s._conn = _FakeConn(ev)
    cert = s.issue_certificate("t1", 123, backend=None)  # no handle → resolver runs
    assert cert.coverage["primary"] == "absent", "server-side probe must set primary"


def test_declared_subsystems_recorded_unverified_but_not_sufficient_alone():
    """A client declaration cannot manufacture a verified entry — declared subsystems
    land 'unverified', so declaring them without a real probe is still refused."""
    ev = []
    svc = _svc(ev, _FakeLedger(ev), dt=None)
    with pytest.raises(EmptyGovernanceCoverage):
        svc.issue_certificate("t1", 123, backend=None, declared_subsystems=["embedding"])
    # but WITH a real probe, the declared subsystem is present and unverified
    ev2 = []
    svc2 = _svc(ev2, _FakeLedger(ev2), dt=None)
    cert = svc2.issue_certificate("t1", 123, backend=_ProbeBackend(present=False),
                                  declared_subsystems=["embedding"])
    assert cert.coverage["embedding"] == "unverified"
    assert cert.coverage["primary"] == "absent"
