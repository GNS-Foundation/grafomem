"""Webhook signature verification for the GRAFOMEM SDK.

Verifies ``X-Grafomem-Signature`` headers on incoming webhook payloads
using HMAC-SHA256 — matching the server's ``webhook_service.py`` signing.

Usage::

    from grafomem.webhooks import verify_signature

    is_valid = verify_signature(
        payload=request.body,
        signature=request.headers["X-Grafomem-Signature"],
        secret="whsec_...",
    )
    if not is_valid:
        return Response(status_code=403)
"""

from __future__ import annotations

import hashlib
import hmac
import time


def verify_signature(
    payload: bytes | str,
    signature: str,
    secret: str,
    *,
    tolerance_seconds: int = 300,
) -> bool:
    """Verify an HMAC-SHA256 webhook signature.

    Parameters
    ----------
    payload : bytes | str
        The raw request body.
    signature : str
        The ``X-Grafomem-Signature`` header value.
        Format: ``t=<unix_ts>,v1=<hex_digest>``
    secret : str
        The webhook signing secret (``whsec_...``).
    tolerance_seconds : int
        Maximum age of the signature in seconds (default 5 minutes).
        Set to 0 to disable timestamp checking.

    Returns
    -------
    bool
        True if the signature is valid and within the time tolerance.
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")

    # Parse signature header: "t=<timestamp>,v1=<hex_digest>"
    parts: dict[str, str] = {}
    for part in signature.split(","):
        if "=" in part:
            key, _, value = part.partition("=")
            parts[key.strip()] = value.strip()

    timestamp_str = parts.get("t", "")
    provided_sig = parts.get("v1", "")

    if not timestamp_str or not provided_sig:
        return False

    # Check timestamp tolerance
    if tolerance_seconds > 0:
        try:
            ts = int(timestamp_str)
        except ValueError:
            return False
        if abs(time.time() - ts) > tolerance_seconds:
            return False

    # Compute expected signature: HMAC-SHA256(secret, timestamp.payload)
    signed_content = f"{timestamp_str}.".encode("utf-8") + payload
    expected = hmac.new(
        secret.encode("utf-8"),
        signed_content,
        hashlib.sha256,
    ).hexdigest()

    # Timing-safe comparison
    return hmac.compare_digest(expected, provided_sig)


def compute_signature(
    payload: bytes | str,
    secret: str,
    *,
    timestamp: int | None = None,
) -> str:
    """Compute a webhook signature for testing.

    Returns the full ``X-Grafomem-Signature`` header value.

    Parameters
    ----------
    payload : bytes | str
        The request body to sign.
    secret : str
        The signing secret.
    timestamp : int | None
        Unix timestamp. Uses ``time.time()`` if not provided.
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")

    ts = timestamp or int(time.time())
    signed_content = f"{ts}.".encode("utf-8") + payload
    digest = hmac.new(
        secret.encode("utf-8"),
        signed_content,
        hashlib.sha256,
    ).hexdigest()

    return f"t={ts},v1={digest}"
