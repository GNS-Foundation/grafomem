"""Conformance suite regression tests.

Runs the GMP conformance suite against GMPReferenceBackend (full profile)
and VectorOnlyBackend (partial profile). Verifies M8 and report generation."""

import pytest

from aml.backends.gmp_reference import GMP_V02_PROFILE, GMPReferenceBackend
from aml.backends.vector_only import VectorOnlyBackend, _stub_embedder
from aml.eval.conformance import run_conformance


def test_gmp_reference_full_conformance():
    """GMPReferenceBackend passes all 7 declared capabilities."""
    profile = run_conformance(
        lambda: GMPReferenceBackend(embed_fn=_stub_embedder()),
        name="GMPReferenceBackend",
        seeds=range(2),
    )
    assert profile.conformance_rate == 1.0
    assert profile.supported == set(GMP_V02_PROFILE)
    assert not profile.violations


def test_vector_only_partial_conformance():
    """VectorOnlyBackend declares AUDIT only — passes AUDIT, no violations."""
    profile = run_conformance(
        lambda: VectorOnlyBackend(embed_fn=_stub_embedder()),
        name="VectorOnlyBackend",
        seeds=range(2),
    )
    assert profile.conformance_rate == 1.0  # declares 1, passes 1
    assert not profile.violations


def test_conformance_rate_property():
    """conformance_rate is correctly computed."""
    profile = run_conformance(
        lambda: GMPReferenceBackend(embed_fn=_stub_embedder()),
        name="test",
        seeds=range(1),
    )
    n_results = len(profile.results)
    n_passed = sum(1 for r in profile.results if r.passed)
    assert profile.conformance_rate == n_passed / n_results


def test_report_generation():
    """ComplianceReport serializes to JSON and Markdown without error."""
    from aml.eval.report import from_profile, to_json, to_markdown

    profile = run_conformance(
        lambda: GMPReferenceBackend(embed_fn=_stub_embedder()),
        name="test",
        seeds=range(1),
    )
    report = from_profile(profile, corpus_hash="test_hash")

    json_str = to_json(report)
    assert '"m8_conformance_rate": 1.0' in json_str

    md_str = to_markdown(report)
    assert "M8 = 1.000" in md_str
    assert "PASS" in md_str
