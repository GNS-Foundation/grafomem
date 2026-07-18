import logging
import time
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel



logger = logging.getLogger("grafomem.hitl.push")

router = APIRouter(prefix="/v1/push", tags=["Push Notifications"])

class PushRegisterRequest(BaseModel):
    approver_id: str
    platform: str
    push_token: str

# Simple in-memory rate limiter: max 5 requests per 60 seconds per key (IP or PK)
_rate_limits: dict[str, list[float]] = defaultdict(list)

def _is_rate_limited(key: str, max_requests: int = 5, window: int = 60) -> bool:
    now = time.monotonic()
    history = [t for t in _rate_limits[key] if now - t < window]
    if len(history) >= max_requests:
        return True
    history.append(now)
    _rate_limits[key] = history
    return False

@router.post("/register")
def register_push_token(
    request: Request,
    body: PushRegisterRequest,
    x_gns_signature: str = Header(...),
    x_gns_timestamp: str = Header(...)
):
    """
    Registers a device push token for an approver.
    Authentication: X-GNS-Signature over 'grafomem.push.register.v1:{approver_id}:{timestamp}'.
    """
    client_ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(f"ip:{client_ip}") or _is_rate_limited(f"pk:{body.approver_id}"):
        raise HTTPException(status_code=429, detail="Too many requests")
    
    # 1. Replay protection
    try:
        ts = int(x_gns_timestamp)
        now_ms = int(time.time() * 1000)
        if abs(now_ms - ts) > 60000:
            raise HTTPException(status_code=401, detail="Timestamp stale or skewed")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid timestamp format")

    # 2. Verify signature
    challenge = f"grafomem.push.register.v1:{body.approver_id}:{x_gns_timestamp}".encode("utf-8")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(body.approver_id))
        pub.verify(bytes.fromhex(x_gns_signature), challenge)
    except Exception as e:
        logger.warning(f"Failed to verify push registration signature: {e}")
        raise HTTPException(status_code=401, detail="Invalid signature")

    # 3. Store validly-signed token
    try:
        pool = request.app.state.db_pool
        with pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO approver_push_tokens (approver_id, platform, push_token)
                VALUES (%s, %s, %s)
                ON CONFLICT (approver_id, push_token) 
                DO UPDATE SET updated_at = timezone('utc', now())
                """,
                (body.approver_id, body.platform, body.push_token)
            )
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error saving push token: {e}")
        raise HTTPException(status_code=500, detail="Failed to store token")
