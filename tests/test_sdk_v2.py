"""GRAFOMEM SDK v2 tests — async client, pagination, and webhook verification.

Run with: pytest tests/test_sdk_v2.py -v
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Webhook verification tests
# ---------------------------------------------------------------------------


class TestWebhookVerification:
    """Tests for grafomem.webhooks.verify_signature / compute_signature."""

    def test_roundtrip(self) -> None:
        """Compute → verify cycle succeeds."""
        from grafomem.webhooks import compute_signature, verify_signature

        payload = b'{"event": "workflow.completed", "workflow_id": "abc123"}'
        secret = "whsec_test_secret_2026"
        ts = int(time.time())

        sig = compute_signature(payload, secret, timestamp=ts)
        assert sig.startswith(f"t={ts},v1=")
        assert verify_signature(payload, sig, secret)

    def test_wrong_secret_fails(self) -> None:
        from grafomem.webhooks import compute_signature, verify_signature

        payload = b'{"event": "test"}'
        sig = compute_signature(payload, "whsec_correct")
        assert not verify_signature(payload, sig, "whsec_wrong")

    def test_tampered_payload_fails(self) -> None:
        from grafomem.webhooks import compute_signature, verify_signature

        original = b'{"amount": 100}'
        sig = compute_signature(original, "whsec_key")
        tampered = b'{"amount": 999}'
        assert not verify_signature(tampered, sig, "whsec_key")

    def test_expired_signature_fails(self) -> None:
        from grafomem.webhooks import compute_signature, verify_signature

        payload = b'{"event": "test"}'
        old_ts = int(time.time()) - 600  # 10 minutes ago
        sig = compute_signature(payload, "whsec_key", timestamp=old_ts)
        # Default tolerance is 300 seconds (5 min)
        assert not verify_signature(payload, sig, "whsec_key")

    def test_tolerance_zero_accepts_old(self) -> None:
        from grafomem.webhooks import compute_signature, verify_signature

        payload = b'{"event": "test"}'
        old_ts = int(time.time()) - 3600  # 1 hour ago
        sig = compute_signature(payload, "whsec_key", timestamp=old_ts)
        # tolerance_seconds=0 disables timestamp check
        assert verify_signature(payload, sig, "whsec_key",
                                tolerance_seconds=0)

    def test_malformed_signature_fails(self) -> None:
        from grafomem.webhooks import verify_signature

        assert not verify_signature(b"data", "garbage", "secret")
        assert not verify_signature(b"data", "", "secret")
        assert not verify_signature(b"data", "t=abc,v1=def", "secret")

    def test_string_payload(self) -> None:
        """String payloads are accepted and encoded as UTF-8."""
        from grafomem.webhooks import compute_signature, verify_signature

        payload_str = '{"event": "test"}'
        secret = "whsec_str_test"
        sig = compute_signature(payload_str, secret)
        assert verify_signature(payload_str, sig, secret)


# ---------------------------------------------------------------------------
# Pagination tests
# ---------------------------------------------------------------------------


class TestPaginator:
    """Tests for grafomem.pagination.Paginator."""

    def test_single_page(self) -> None:
        from grafomem.pagination import Paginator

        mock_fn = MagicMock(return_value={
            "decisions": [{"id": "a"}, {"id": "b"}],
            "total": 2,
        })

        items = list(Paginator(mock_fn, page_size=50))
        assert len(items) == 2
        assert items[0]["id"] == "a"
        mock_fn.assert_called_once_with(limit=50, offset=0)

    def test_multi_page(self) -> None:
        from grafomem.pagination import Paginator

        page1 = {"items": [{"n": i} for i in range(3)]}
        page2 = {"items": [{"n": i} for i in range(3, 5)]}

        mock_fn = MagicMock(side_effect=[page1, page2])
        items = list(Paginator(mock_fn, page_size=3))
        assert len(items) == 5
        assert mock_fn.call_count == 2

    def test_max_items(self) -> None:
        from grafomem.pagination import Paginator

        mock_fn = MagicMock(return_value={
            "data": [{"n": i} for i in range(100)],
        })

        items = list(Paginator(mock_fn, page_size=100, max_items=10))
        assert len(items) == 10

    def test_empty_response(self) -> None:
        from grafomem.pagination import Paginator

        mock_fn = MagicMock(return_value={"items": []})
        items = list(Paginator(mock_fn, page_size=50))
        assert len(items) == 0

    def test_explicit_list_key(self) -> None:
        from grafomem.pagination import Paginator

        mock_fn = MagicMock(return_value={
            "total": 1,
            "decisions": [{"id": "x"}],
        })

        items = list(Paginator(mock_fn, page_size=50, list_key="decisions"))
        assert len(items) == 1


class TestAsyncPaginator:
    """Tests for grafomem.pagination.AsyncPaginator."""

    def test_async_pagination(self) -> None:
        from grafomem.pagination import AsyncPaginator

        page1 = {"items": [{"n": 1}, {"n": 2}]}
        page2 = {"items": [{"n": 3}]}
        pages = [page1, page2]

        async def mock_list(**kwargs):
            return pages.pop(0) if pages else {"items": []}

        async def run():
            results = []
            async for item in AsyncPaginator(mock_list, page_size=2):
                results.append(item)
            return results

        items = asyncio.run(run())
        assert len(items) == 3

    def test_async_max_items(self) -> None:
        from grafomem.pagination import AsyncPaginator

        async def mock_list(**kwargs):
            return {"items": [{"n": i} for i in range(50)]}

        async def run():
            results = []
            async for item in AsyncPaginator(mock_list, page_size=50,
                                              max_items=5):
                results.append(item)
            return results

        items = asyncio.run(run())
        assert len(items) == 5


# ---------------------------------------------------------------------------
# Async client tests
# ---------------------------------------------------------------------------


class TestGrafomemAsyncClient:
    """Tests for the async client structure."""

    def test_import(self) -> None:
        from grafomem import GrafomemAsyncClient
        client = GrafomemAsyncClient(api_key="gfm_test")
        assert repr(client).startswith("GrafomemAsyncClient")

    def test_service_properties(self) -> None:
        from grafomem import GrafomemAsyncClient
        client = GrafomemAsyncClient(api_key="gfm_test")

        # All 9 service namespaces must be present
        assert client.stores is not None
        assert client.memories is not None
        assert client.governance is not None
        assert client.orchestrator is not None
        assert client.decisions is not None
        assert client.erasure is not None
        assert client.reports is not None
        assert client.llm is not None
        assert client.webhooks is not None

    def test_context_manager(self) -> None:
        from grafomem import GrafomemAsyncClient

        async def run():
            async with GrafomemAsyncClient(api_key="gfm_test") as client:
                assert client.stores is not None

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Version check
# ---------------------------------------------------------------------------


class TestSDKVersion:
    def test_version_is_0_2_0(self) -> None:
        import grafomem
        assert grafomem.__version__ == "0.2.0"

    def test_all_exports_present(self) -> None:
        import grafomem
        expected = [
            "GrafomemClient", "GrafomemAsyncClient",
            "Paginator", "AsyncPaginator",
            "verify_signature", "compute_signature",
        ]
        for name in expected:
            assert hasattr(grafomem, name), f"Missing export: {name}"
