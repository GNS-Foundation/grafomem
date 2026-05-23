"""W10 concurrency oracle and runner-bridge tests.

Verifies the anomaly lattice, permissible-outcome oracle, and the
trace → backend → oracle bridge across the 5-store isolation spectrum."""

import pytest

from aml.backends.interface import IsolationLevel
from aml.eval.concurrency import (
    TxnAnomaly,
    ALLOWED,
)


# ---------------------------------------------------------------------------
# Oracle / lattice tests
# ---------------------------------------------------------------------------

def test_anomaly_allowed_sets():
    """Serializable allows nothing; snapshot allows write-skew; read_committed allows more."""
    assert len(ALLOWED[IsolationLevel.SERIALIZABLE]) == 0
    assert TxnAnomaly.WRITE_SKEW in ALLOWED[IsolationLevel.SNAPSHOT]
    assert TxnAnomaly.LOST_UPDATE in ALLOWED[IsolationLevel.READ_COMMITTED]


def test_serializable_strictest():
    """Serializable's allowed set is a subset of every other level's."""
    ser = ALLOWED[IsolationLevel.SERIALIZABLE]
    for level, allowed in ALLOWED.items():
        assert ser <= allowed, f"Serializable not subset of {level}"


def test_snapshot_subset_of_read_committed():
    """Snapshot's allowed set is a subset of read_committed's."""
    snap = ALLOWED[IsolationLevel.SNAPSHOT]
    rc = ALLOWED[IsolationLevel.READ_COMMITTED]
    assert snap <= rc


# ---------------------------------------------------------------------------
# Runner-bridge tests (require the isolation backends + concurrency_runner)
# ---------------------------------------------------------------------------

def _run_store(store_cls):
    """Helper: run W10 trace through the bridge for a single store."""
    from aml.generator.trace import Difficulty
    from aml.generator.workloads.w10 import generate_w10
    from aml.eval.concurrency_runner import run_w10, summarize

    tr = generate_w10(seed=0, difficulty=Difficulty.EASY)
    store = store_cls()
    verdicts = run_w10(store, tr)
    return summarize(verdicts)


def test_isolation_spectrum_smoke():
    """All 5 isolation stores run through the bridge without error."""
    from aml.backends.isolation_backends import (
        SerializableStore,
        SnapshotStore,
        ReadCommittedStore,
        NoIsolationStore,
        ResurrectingStore,
    )

    for cls in [SerializableStore, SnapshotStore, ReadCommittedStore,
                NoIsolationStore, ResurrectingStore]:
        summary = _run_store(cls)
        assert summary is not None, f"{cls.__name__} returned None"
        assert "overall" in summary, f"{cls.__name__} missing 'overall'"
        assert "combined_achieved" in summary, f"{cls.__name__} missing 'combined_achieved'"


def test_serializable_passes():
    """SerializableStore achieves serializable with no downgrades."""
    from aml.backends.isolation_backends import SerializableStore
    summary = _run_store(SerializableStore)
    assert summary["overall"] == "OK"
    assert summary["combined_achieved"] == IsolationLevel.SERIALIZABLE


def test_over_claimer_downgraded():
    """NoIsolationStore claims serializable but gets downgraded."""
    from aml.backends.isolation_backends import NoIsolationStore
    summary = _run_store(NoIsolationStore)
    assert summary["overall"] == "DOWNGRADE"


def test_resurrecting_store_caught():
    """ResurrectingStore triggers §10.4 resurrection detection."""
    from aml.backends.isolation_backends import ResurrectingStore
    summary = _run_store(ResurrectingStore)
    assert summary["overall"] == "VIOLATION"
    # At least one group must have RESURRECTION anomaly
    assert any(
        group[0] == "resurrection" for group in summary["by_group"]
    )
