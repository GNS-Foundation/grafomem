"""Pytest wrapper for the gitleaks rule corpus (task d).

The authoritative gate is the CI `secret-scan` job (which installs gitleaks and runs
tests/secret_scanner/run_corpus_check.py). This wrapper lets `pytest` exercise it
locally; it SKIPS when gitleaks isn't installed (e.g. the pytest CI job, which does not
install gitleaks — the dedicated secret-scan job does).
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CHECK = os.path.join(_HERE, "secret_scanner", "run_corpus_check.py")


@pytest.mark.skipif(shutil.which("gitleaks") is None,
                    reason="gitleaks not installed (authoritative run is the CI secret-scan job)")
def test_secret_rule_corpus():
    """All synthetic positives caught, zero negatives flagged (exit 0 from the checker)."""
    proc = subprocess.run(["python3", _CHECK], capture_output=True, text=True)
    assert proc.returncode == 0, f"corpus check failed:\n{proc.stdout}\n{proc.stderr}"
