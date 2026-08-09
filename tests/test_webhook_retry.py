"""1B-3 — webhook delivery retry.

`retry_delivery` re-sends a past delivery as a NEW attempt, reusing the stored
event_type + payload but the webhook's CURRENT config. The delivery loader is
id-only, so ownership (delivery ∈ webhook ∈ tenant) is enforced in the service —
these tests are the IDOR gate. The network primitive `_deliver_with_retry` is
monkeypatched so no real HTTP happens.
"""
from __future__ import annotations

import uuid

import pytest

from aml.cloud.webhook_service import DeliveryStatus, WebhookService

TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"


def _tenant() -> str:
    return f"wh-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def svc():
    s = WebhookService(TEST_DB_URL)
    s.ensure_schema()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def capture(svc, monkeypatch):
    """Replace the real HTTP send with a capture that marks the row delivered."""
    seen = {}

    def fake_deliver(delivery_id, config, event_type, payload):
        seen["delivery_id"] = delivery_id
        seen["config"] = config
        seen["event_type"] = event_type
        seen["payload"] = payload
        svc._update_delivery(delivery_id, DeliveryStatus.DELIVERED, attempts=1, response_code=200)

    monkeypatch.setattr(svc, "_deliver_with_retry", fake_deliver)
    return seen


def _seed_delivery(svc, tenant, *, event_type="governance.denied", payload=None):
    cfg = svc.register(tenant, "https://old.example.com/hook", [event_type], "orig")
    did = uuid.uuid4().hex[:24]
    svc._persist_delivery(did, cfg, event_type, payload or {"policy": "p1", "n": 7})
    return cfg, did


def test_retry_creates_new_delivery_reusing_event_and_payload(svc, capture):
    t = _tenant()
    cfg, orig_id = _seed_delivery(svc, t, payload={"policy": "p1", "n": 7})

    new = svc.retry_delivery(cfg.webhook_id, orig_id, t)

    assert new is not None
    assert new.delivery_id != orig_id                       # a fresh attempt row
    assert capture["event_type"] == "governance.denied"     # original event reused
    assert capture["payload"] == {"policy": "p1", "n": 7}    # original payload reused
    assert svc._get_delivery(orig_id) is not None           # history preserved


def test_retry_uses_current_config_not_stale_snapshot(svc, capture):
    t = _tenant()
    cfg, orig_id = _seed_delivery(svc, t)
    # mutate the webhook's URL after the original delivery was recorded
    conn = svc._get_conn()
    conn.execute("UPDATE webhook_configs SET url = %s WHERE webhook_id = %s",
                 ("https://new.example.com/hook", cfg.webhook_id))

    svc.retry_delivery(cfg.webhook_id, orig_id, t)

    assert capture["config"].url == "https://new.example.com/hook"


def test_retry_rejects_other_tenant(svc, capture):
    owner, attacker = _tenant(), _tenant()
    cfg, orig_id = _seed_delivery(svc, owner)
    # IDOR: another tenant must not retry the owner's delivery
    assert svc.retry_delivery(cfg.webhook_id, orig_id, attacker) is None
    assert "delivery_id" not in capture                     # send never invoked


def test_retry_rejects_wrong_webhook(svc, capture):
    t = _tenant()
    cfg, orig_id = _seed_delivery(svc, t)
    other = svc.register(t, "https://other.example.com/hook", ["workflow.completed"], "other")
    # the delivery does not belong to `other` webhook
    assert svc.retry_delivery(other.webhook_id, orig_id, t) is None


def test_retry_unknown_delivery_returns_none(svc, capture):
    t = _tenant()
    cfg, _ = _seed_delivery(svc, t)
    assert svc.retry_delivery(cfg.webhook_id, "does-not-exist", t) is None
