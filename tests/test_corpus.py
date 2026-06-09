"""Corpus integrity tests.

Verifies that generate_corpus.py produces deterministic output (R3) and
that the corpus.lock hashes match the traces on disk."""

import hashlib
import json
from pathlib import Path

import pytest

CORPUS_DIR = Path(__file__).parent.parent / "corpus"
LOCK_PATH = CORPUS_DIR / "corpus.lock"
TRACES_DIR = CORPUS_DIR / "traces"


@pytest.fixture
def lock():
    if not LOCK_PATH.exists():
        pytest.skip("corpus.lock not found")
    return json.loads(LOCK_PATH.read_text())


def test_lock_metadata(lock):
    """corpus.lock has required fields."""
    assert lock["name"] == "grafomem-bench-v1.0.0"
    assert lock["n_traces"] == 150
    assert "corpus_hash" in lock
    assert "trace_hashes" in lock
    assert "workload_hashes" in lock


def test_trace_count(lock):
    """150 traces across 10 workloads × 5 seeds × 3 difficulties."""
    assert len(lock["trace_hashes"]) == 150


def test_workload_coverage(lock):
    """All 10 workloads present in the lock."""
    expected = {"W1", "W2", "W3", "W4", "W5", "W6", "W7", "W8", "W9", "W10"}
    actual = set(lock["workload_hashes"].keys())
    assert actual == expected


def test_trace_hashes_match(lock):
    """Every trace on disk matches its content-hash in the lock.
    Uses the same hashing logic as generate_corpus.py: strip trace_id +
    generated_at, re-canonicalize, BLAKE2b-256."""
    if not TRACES_DIR.exists():
        pytest.skip("traces directory not found — run generate_corpus.py first")

    mismatches = []
    for trace_name, expected_hash in lock["trace_hashes"].items():
        trace_file = TRACES_DIR / f"{trace_name}.jsonl"
        if not trace_file.exists():
            mismatches.append(f"MISSING: {trace_name}")
            continue
        trace_dict = json.loads(trace_file.read_text(encoding="utf-8"))
        trace_dict.pop("trace_id", None)
        trace_dict.pop("generated_at", None)
        canonical = json.dumps(trace_dict, sort_keys=True, separators=(",", ":"))
        actual = hashlib.blake2b(canonical.encode("utf-8"), digest_size=32).hexdigest()
        if actual != expected_hash:
            mismatches.append(f"{trace_name}: expected={expected_hash[:16]}... actual={actual[:16]}...")

    assert not mismatches, f"{len(mismatches)} hash mismatch(es):\n" + "\n".join(mismatches)
