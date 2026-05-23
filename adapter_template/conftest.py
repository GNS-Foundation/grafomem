"""Conformance test fixture for your GMP backend.

Run with: pytest --tb=short
"""
import subprocess
import sys

def test_conformance_passes():
    """Run grafomem conformance against MyBackend and assert M8 = 1.0."""
    result = subprocess.run(
        [sys.executable, "-m", "aml.cli", "conformance",
         "-b", "my_backend:MyBackend", "-s", "2"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"Conformance failed:\n{result.stdout}\n{result.stderr}"
    assert "M8 conformance rate: 1.000" in result.stdout


def test_check_passes():
    """Run grafomem check against MyBackend."""
    result = subprocess.run(
        [sys.executable, "-m", "aml.cli", "check",
         "-b", "my_backend:MyBackend"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"Check failed:\n{result.stdout}\n{result.stderr}"
    assert "structurally conformant" in result.stdout
