"""Must-fail regression for the production-target guard (see aml.prod_target_guard).

The conformance/resilience suites create real tenants; pointed at production they
seed prod with test data (9 such tenants, 31 May–28 Jun — the ones I0c backfilled).
The guard refuses a prod host unless prod is explicitly allowed per invocation.

`test_prod_host_without_allow_raises` is the must-fail case: it FAILS (no SystemExit)
if the guard is removed or weakened — watch it fail against a no-op guard first.
"""
import pytest

from aml.prod_target_guard import guard_not_prod, is_prod_host, prod_allowed, PROD_HOSTS

PROD_URL = "https://api.grafomem.com"
PROD_URL_2 = "https://grafomem-production.up.railway.app"
STAGING_URL = "https://staging.grafomem.com"
LOCAL_URL = "http://localhost:8080"


# ── must-fail: a prod host with no explicit allow is refused ─────────────────
@pytest.mark.parametrize("url", [PROD_URL, PROD_URL_2])
def test_prod_host_without_allow_raises(url, monkeypatch):
    monkeypatch.delenv("GRAFOMEM_ALLOW_PROD", raising=False)
    with pytest.raises(SystemExit):
        guard_not_prod(url, argv=["prog"])  # no --allow-prod, no env


# ── the per-invocation flag is the intended override ─────────────────────────
def test_allow_prod_flag_permits(monkeypatch):
    monkeypatch.delenv("GRAFOMEM_ALLOW_PROD", raising=False)
    guard_not_prod(PROD_URL, argv=["prog", "--allow-prod"])  # no raise


# ── the env var is the secondary (CI) override ───────────────────────────────
def test_allow_prod_env_permits(monkeypatch):
    monkeypatch.setenv("GRAFOMEM_ALLOW_PROD", "1")
    guard_not_prod(PROD_URL, argv=["prog"])  # no raise


# ── non-prod hosts are always allowed, override or not ───────────────────────
@pytest.mark.parametrize("url", [STAGING_URL, LOCAL_URL])
def test_non_prod_host_never_raises(url, monkeypatch):
    monkeypatch.delenv("GRAFOMEM_ALLOW_PROD", raising=False)
    guard_not_prod(url, argv=["prog"])  # no raise


# ── helper unit coverage ─────────────────────────────────────────────────────
def test_is_prod_host_identifies_prod():
    assert is_prod_host(PROD_URL) and is_prod_host(PROD_URL_2)
    assert not is_prod_host(STAGING_URL) and not is_prod_host(LOCAL_URL)


def test_prod_allowed_reads_flag_and_env(monkeypatch):
    monkeypatch.delenv("GRAFOMEM_ALLOW_PROD", raising=False)
    assert not prod_allowed(argv=["prog"])
    assert prod_allowed(argv=["prog", "--allow-prod"])
    monkeypatch.setenv("GRAFOMEM_ALLOW_PROD", "1")
    assert prod_allowed(argv=["prog"])


def test_prod_hosts_are_the_known_set():
    assert set(PROD_HOSTS) == {"api.grafomem.com", "grafomem-production.up.railway.app"}
