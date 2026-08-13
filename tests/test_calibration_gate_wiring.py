"""B2b Gate-1 — engine resolution + calibration endpoint wiring.

The load-bearing property: the gate is NEUTRAL (review_gate=None / cap_k=None) for any
tenant lacking BOTH an enabled cgr_gate_config row AND ≥1 agent_calibration weight — so
corp and every un-configured tenant score BYTE-IDENTICALLY to v1. These tests prove the
resolution never turns the gate on by accident, and that None == v1 in score_agent.
"""
from __future__ import annotations

from types import SimpleNamespace

from aml.cgr.gate import resolve_review_gate, review_gate_g
from aml.cgr.scoring import WeightingConfig, score_agent


# ── a minimal dict_row/tuple-agnostic fake connection ──
class _FakeCursor:
    def __init__(self, script):
        self._script = script  # list of (fetchone_result, fetchall_result), consumed per execute
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "cgr_gate_config" in sql:
            self._last = ("one", self._script.get("gate_config"))
        elif "agent_calibration" in sql:
            self._last = ("all", self._script.get("calibration"))
        else:
            self._last = None

    def fetchone(self):
        return self._last[1] if self._last and self._last[0] == "one" else None

    def fetchall(self):
        return self._last[1] if self._last and self._last[0] == "all" else []


class _FakeConn:
    def __init__(self, script):
        self._script = script

    def cursor(self):
        return _FakeCursor(self._script)


# ── resolution: neutral cases (the byte-identical guarantee) ──
def test_resolve_neutral_when_no_config():
    # no cgr_gate_config row ⇒ gate OFF regardless of calibration data
    conn = _FakeConn({"gate_config": None, "calibration": [("k1", 0.9)]})
    assert resolve_review_gate(conn, "t") == (None, None)


def test_resolve_neutral_when_config_disabled():
    # enabled=False ⇒ off
    conn = _FakeConn({"gate_config": {"tau": 0.1, "cap_k": 3.0, "enabled": False},
                      "calibration": [("k1", 0.9)]})
    assert resolve_review_gate(conn, "t") == (None, None)


def test_resolve_neutral_when_config_but_no_calibration():
    conn = _FakeConn({"gate_config": {"tau": 0.1, "cap_k": 3.0, "enabled": True},
                      "calibration": []})
    assert resolve_review_gate(conn, "t") == (None, None)


def test_resolve_neutral_when_tables_missing():
    class _Boom:
        def cursor(self):
            raise RuntimeError("relation cgr_gate_config does not exist")
    assert resolve_review_gate(_Boom(), "t") == (None, None)


# ── resolution: gate ON only when BOTH config + calibration exist ──
def test_resolve_gate_on_with_config_and_calibration():
    conn = _FakeConn({"gate_config": {"tau": 0.10, "cap_k": 3.0, "enabled": True},
                      "calibration": [("k-high", 0.8), ("k-low", 0.05)]})
    gate, cap_k = resolve_review_gate(conn, "t")
    assert gate is not None and cap_k == 3.0
    assert abs(gate("k-high") - review_gate_g(0.8, 0.10)) < 1e-9
    assert gate("k-low") == 0.0          # below τ ⇒ floored
    assert gate("unknown") == 0.0        # cold-start fail-safe


# ── byte-identical: review_gate=None/cap_k=None == v1 (verifiable + review channels) ──
def _cert(ref):
    return SimpleNamespace(decision="certify", verifiability_tag="judgment", invoice_ref=ref)


def test_gate_off_is_byte_identical_to_v1():
    refs = [f"inv-{i}" for i in range(6)]
    outcomes = {r: ("paid" if i % 2 == 0 else "default") for i, r in enumerate(refs)}
    reviews = [("inv-x", "rev-a", 1.0), ("inv-y", "rev-b", 0.0)]
    reviewer_w = {"rev-a": 0.7, "rev-b": 0.4}
    decisions = [_cert(r) for r in refs] + [_cert("inv-x"), _cert("inv-y")]
    v1 = score_agent("agent", decisions, outcomes, reviews, reviewer_w, None,
                     as_of="2026-08-13T00:00:00+00:00", weighting=WeightingConfig())
    off = score_agent("agent", decisions, outcomes, reviews, reviewer_w, None,
                      as_of="2026-08-13T00:00:00+00:00",
                      weighting=WeightingConfig(review_gate=None, review_cap_k=None))
    assert v1.cgr_score == off.cgr_score
    assert v1.confidence == off.confidence and v1.n_resolved == off.n_resolved
