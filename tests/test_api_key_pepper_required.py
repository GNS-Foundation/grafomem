"""Hash-at-rest, PR 5 open-gate #3: GRAFOMEM_API_KEY_PEPPER is REQUIRED at process startup (fail closed).

MUST-FAIL-FIRST: on main before this change, create_app(db_url=...) with the pepper unset and UNSAFE_LOCAL_DEV
unset returns an app (the process would boot and, after PR 5, authenticate nothing). With the change it raises
RuntimeError before the cloud layer is built — outside the cloud-layer `try/except Exception` that logs and
continues, so the process never starts and the deploy fails while the previous version keeps serving.

The check runs before any DB work, so these tests need no database: db_url is a syntactically valid URL that is
never connected to (create_app defers connections to the lifespan / ensure_schema)."""
from __future__ import annotations

import logging
import os

import pytest

from aml.server.app import create_app


def _check():
    from aml.server.app import _require_api_key_pepper   # lazy: the module must import on main so the first test can FAIL there
    return _require_api_key_pepper()

DB_URL = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:dev@localhost:5432/grafomem")


def test_pepper_missing_fails_closed(monkeypatch):
    """No pepper, no dev escape → RuntimeError at startup, before the cloud layer (must fail on main)."""
    monkeypatch.delenv("GRAFOMEM_API_KEY_PEPPER", raising=False)
    monkeypatch.delenv("UNSAFE_LOCAL_DEV", raising=False)
    monkeypatch.setenv("GRAFOMEM_MASTER_KEY", "00" * 32)
    with pytest.raises(RuntimeError, match="GRAFOMEM_API_KEY_PEPPER must be set in environment"):
        create_app(db_url=DB_URL, spec_only=True)


def test_pepper_empty_counts_as_missing(monkeypatch):
    monkeypatch.setenv("GRAFOMEM_API_KEY_PEPPER", "   ")
    monkeypatch.delenv("UNSAFE_LOCAL_DEV", raising=False)
    with pytest.raises(RuntimeError, match="GRAFOMEM_API_KEY_PEPPER must be set"):
        _check()


def test_pepper_present_passes(monkeypatch):
    monkeypatch.setenv("GRAFOMEM_API_KEY_PEPPER", "a-test-pepper")
    monkeypatch.delenv("UNSAFE_LOCAL_DEV", raising=False)
    _check()   # no raise


def test_unsafe_local_dev_bypasses_with_warning(monkeypatch, caplog):
    """The dev escape mirrors GRAFOMEM_MASTER_KEY: allowed, but loudly."""
    monkeypatch.delenv("GRAFOMEM_API_KEY_PEPPER", raising=False)
    monkeypatch.setenv("UNSAFE_LOCAL_DEV", "true")
    with caplog.at_level(logging.WARNING):
        _check()
    assert any("GRAFOMEM_API_KEY_PEPPER is not set" in r.getMessage() for r in caplog.records)


def test_check_is_outside_the_cloud_layer_try():
    """The check must not sit inside the cloud-layer `try` whose `except Exception` logs and continues
    (app.py: 'Cloud layer failed to initialize'), or a pepper-less process would still boot."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "aml", "server", "app.py")).read()
    call = src.index("_require_api_key_pepper()\n")
    block_try = src.index("        try:\n            # In spec_only mode we skip ensure_schema()")
    assert call < block_try, "the pepper check must run before the swallowing try block"
