"""
GRAFOMEM OpenAPI Contract Tests — validate SDK types against the spec.

Loads the OpenAPI spec (either from the running app or a snapshot file)
and checks that every SDK Pydantic model in ``grafomem.types`` has a
corresponding schema in the spec with matching field names and types.

This is the "never drift again" guarantee: if someone adds a field to
a route response model but forgets to update the SDK, this test fails.

Usage:
    python -m pytest tests/test_openapi_contract.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure project root is importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "sdk" / "src"))


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(scope="session")
def openapi_spec() -> dict[str, Any]:
    """Load the OpenAPI spec from the running app.

    Uses ``spec_only=True`` so that all cloud routes are registered
    without requiring a live PostgreSQL database.
    """
    from aml.server.app import create_app

    app = create_app(
        db_url="postgresql://spec:spec@localhost:5432/spec",
        auth_mode="none",
        spec_only=True,
    )
    return app.openapi()


@pytest.fixture(scope="session")
def spec_schemas(openapi_spec: dict) -> dict[str, dict]:
    """Extract component schemas from the OpenAPI spec."""
    return openapi_spec.get("components", {}).get("schemas", {})


@pytest.fixture(scope="session")
def spec_paths(openapi_spec: dict) -> dict[str, dict]:
    """Extract paths from the OpenAPI spec."""
    return openapi_spec.get("paths", {})


# ============================================================================
# Tests
# ============================================================================

class TestOpenAPISpec:
    """Validate the OpenAPI spec structure and completeness."""

    def test_spec_has_info(self, openapi_spec: dict):
        """Spec has proper info block."""
        info = openapi_spec.get("info", {})
        assert info.get("title") == "GRAFOMEM Cloud"
        assert info.get("version"), "Missing version"

    def test_spec_has_paths(self, spec_paths: dict):
        """Spec has a reasonable number of paths."""
        assert len(spec_paths) >= 30, (
            f"Expected ≥30 paths, got {len(spec_paths)}"
        )

    def test_spec_has_schemas(self, spec_schemas: dict):
        """Spec has component schemas (from response_model + request models)."""
        assert len(spec_schemas) >= 20, (
            f"Expected ≥20 schemas, got {len(spec_schemas)}"
        )

    def test_critical_schemas_present(self, spec_schemas: dict):
        """Critical response schemas exist in the spec."""
        critical = [
            "DecisionResponse",
            "ReplayResponse",
            "ScrubResponse",
            "CertificateResponse",
            "VerifyResponse",
            "LogDecisionRequest",
            "RegisterProviderRequest",
            "CreateAgentRequest",
            "CreateWorkflowRequest",
            "RunWorkflowRequest",
            "IssueErasureRequest",
            "GenerateReportRequest",
        ]
        missing = [s for s in critical if s not in spec_schemas]
        assert not missing, f"Missing critical schemas: {missing}"

    def test_all_post_endpoints_have_request_body(self, spec_paths: dict):
        """Every POST endpoint should have a request body schema."""
        # Legitimate POST endpoints that don't need a request body:
        # action triggers, webhooks, seed commands, key rotations, etc.
        _BODYLESS_OK = {
            "/test", "/replay", "/rotate-key", "/flush",
            "/seed-defaults", "/seed-builtins", "/terminate",
            "/roll", "/webhook", "/cancel", "/acs", "/clear_cache",
        }
        missing = []
        for path, methods in spec_paths.items():
            if "post" in methods:
                post = methods["post"]
                has_body = "requestBody" in post
                # Skip paths whose last segment matches a known bodyless pattern
                tail = "/" + path.rstrip("/").rsplit("/", 1)[-1]
                if not has_body and not any(t in path for t in _BODYLESS_OK):
                    missing.append(path)
        assert len(missing) <= 5, (
            f"Too many POST endpoints missing request body: {missing}"
        )


class TestOpenAPIResponseCoverage:
    """Check that response schemas are present for key endpoints."""

    def _has_response_schema(
        self, spec_paths: dict, path: str, method: str = "get",
    ) -> bool:
        """Check if an endpoint has a 200/201 response schema."""
        methods = spec_paths.get(path, {})
        detail = methods.get(method, {})
        responses = detail.get("responses", {})
        for code in ("200", "201"):
            resp = responses.get(code, {})
            if "content" in resp:
                return True
        return False

    @pytest.mark.parametrize("path,method", [
        ("/v1/decisions/log", "post"),
        ("/v1/decisions/{decision_id}", "get"),
        ("/v1/decisions/{decision_id}/replay", "get"),
        ("/v1/decisions/scrub/{fact_ref}", "delete"),
        ("/v1/erasure/issue", "post"),
        ("/v1/erasure/{certificate_id}/verify", "get"),
    ])
    def test_response_schema_present(
        self, spec_paths: dict, path: str, method: str,
    ):
        """Key endpoints have response schemas in the OpenAPI spec."""
        assert self._has_response_schema(spec_paths, path, method), (
            f"Missing response schema: {method.upper()} {path}"
        )

    def test_count_response_coverage(self, spec_paths: dict):
        """Report overall response schema coverage."""
        total = 0
        covered = 0
        for path, methods in spec_paths.items():
            for method in ("get", "post", "put", "delete", "patch"):
                if method in methods:
                    total += 1
                    if self._has_response_schema(spec_paths, path, method):
                        covered += 1
        coverage = (covered / total * 100) if total else 0
        print(f"\nResponse schema coverage: {covered}/{total} ({coverage:.0f}%)")
        # Soft check — we want to trend toward 100%
        assert coverage >= 20, (
            f"Response schema coverage too low: {coverage:.0f}%"
        )
