"""tests/conftest.py must refuse to run the suite against a non-local database.

The suite truncates and drops tables; it must only ever point at localhost (127.0.0.1 / ::1). The
database comes from GRAFOMEM_TEST_DB_URL (default: the local dev URL); a remote host makes conftest
exit before any connection. Checked in a subprocess so the exit of that conftest cannot take this
run down with it.
"""
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _collect_with(url: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GRAFOMEM_TEST_DB_URL"] = url
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "tests/test_conftest_refuses_remote_db.py"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )


def test_remote_host_makes_conftest_exit():
    r = _collect_with("postgresql://grafomem:dev@db.example.invalid:5432/grafomem")
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "refusing" in out and "db.example.invalid" in out, out
    assert "collected" not in out.lower() or "0 tests" in out, "nothing may be collected against a remote host"


def test_local_host_is_accepted():
    r = _collect_with("postgresql://grafomem:dev@127.0.0.1:5432/grafomem")
    out = r.stdout + r.stderr
    assert "refusing" not in out, out
