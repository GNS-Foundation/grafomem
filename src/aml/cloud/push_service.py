import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional

import httpx
import jwt

logger = logging.getLogger("grafomem.hitl.push")


class PushDispatchService:
    """
    Handles APNs push dispatch for HITL requests.
    Holds a single HTTP/2 connection and provider JWT, reused across dispatches.
    Dispatch runs in a fire-and-forget background thread.
    """

    def __init__(self, db_pool):
        self.db_pool = db_pool
        self.auth_key = os.environ.get("APNS_AUTH_KEY")
        self.key_id = os.environ.get("APNS_KEY_ID")
        self.team_id = os.environ.get("APNS_TEAM_ID")
        self.topic = os.environ.get("APNS_TOPIC")
        self.env = os.environ.get("APNS_ENV", "sandbox")

        self.client = httpx.Client(http2=True)
        self._jwt_token: Optional[str] = None
        self._jwt_generated_at: float = 0
        self._jwt_lock = threading.Lock()

        if self.env == "production":
            self.base_url = "https://api.push.apple.com"
        else:
            self.base_url = "https://api.sandbox.push.apple.com"

    def close(self):
        self.client.close()

    def _get_jwt(self) -> str:
        if not self.auth_key or not self.key_id or not self.team_id:
            logger.warning("APNS credentials missing; push dispatch aborted")
            return ""

        now = time.time()
        with self._jwt_lock:
            # APNs rejects tokens older than 1 hour. Regenerate every 55 minutes.
            if self._jwt_token is None or now - self._jwt_generated_at > 3300:
                headers = {"alg": "ES256", "kid": self.key_id}
                payload = {"iss": self.team_id, "iat": int(now)}
                self._jwt_token = jwt.encode(payload, self.auth_key, algorithm="ES256", headers=headers)
                self._jwt_generated_at = now
            return self._jwt_token

    def dispatch_background(self, tenant_id: str, request_id: str, expires_at: datetime):
        """Fire-and-forget background dispatcher."""
        threading.Thread(
            target=self._dispatch_sync,
            args=(tenant_id, request_id, expires_at),
            daemon=True
        ).start()

    def _dispatch_sync(self, tenant_id: str, request_id: str, expires_at: datetime):
        """Query pending requests for badge counts and dispatch APNs to active approvers."""
        jwt_token = self._get_jwt()
        if not jwt_token:
            return

        try:
            with self.db_pool.connection() as conn:
                # Get active approvers and their push tokens
                rows = conn.execute(
                    """
                    SELECT a.approver_id, t.push_token
                    FROM hitl_approvers a
                    JOIN approver_push_tokens t ON a.approver_id = t.approver_id
                    WHERE a.tenant_id = %s AND a.active = TRUE
                    """, (tenant_id,)
                ).fetchall()

                if not rows:
                    return

                # Query badge counts (pending requests count for each approver)
                badge_rows = conn.execute(
                    """
                    SELECT a.approver_id, COUNT(r.request_id) as pending_count
                    FROM hitl_approvers a
                    LEFT JOIN hitl_approval_requests r 
                      ON a.tenant_id = r.tenant_id 
                     AND r.status = 'pending' 
                     AND r.expires_at > timezone('utc', now())
                    WHERE a.tenant_id = %s AND a.active = TRUE
                    GROUP BY a.approver_id
                    """, (tenant_id,)
                ).fetchall()
                
                badge_map = {b["approver_id"]: b["pending_count"] for b in badge_rows}

            for row in rows:
                approver_id = row["approver_id"]
                token = row["push_token"]
                badge_count = badge_map.get(approver_id, 1)
                self._send_push(token, badge_count, request_id, expires_at)

        except Exception as e:
            logger.error(f"Failed during APNs dispatch query: {e}", exc_info=True)

    def _send_push(self, token: str, badge_count: int, request_id: str, expires_at: datetime):
        jwt_token = self._get_jwt()
        url = f"{self.base_url}/3/device/{token}"
        
        headers = {
            "authorization": f"bearer {jwt_token}",
            "apns-topic": self.topic or "com.grafomem.app",
            "apns-push-type": "alert",
            "apns-expiration": str(int(expires_at.timestamp())),
            "apns-collapse-id": request_id,
        }

        payload = {
            "aps": {
                "alert": {
                    "body": "You have an approval request"
                },
                "badge": badge_count,
            },
            "data": {
                "request_id": request_id,
                "link": f"grafomem:hitl:{request_id}"
            }
        }

        try:
            resp = self.client.post(url, headers=headers, json=payload, timeout=5.0)
            if resp.status_code == 200:
                logger.debug(f"Push successful to {token}")
            elif resp.status_code in (410, 400):
                logger.warning(f"APNs token invalid ({resp.status_code}) env={self.env}. Pruning token.")
                with self.db_pool.connection() as conn:
                    conn.execute("DELETE FROM approver_push_tokens WHERE push_token = %s", (token,))
            else:
                logger.warning(f"APNs push failed: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"Error sending APNs push to {token}: {e}")
