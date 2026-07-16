import pytest
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric import ed25519

from aml.cloud.hitl_routes import create_hitl_router

class MockOrchestrator:
    def __init__(self):
        self.resumed = False

    def resume_workflow(self, workflow_id: str, approved: bool):
        self.resumed = True


class MockGcrumbs:
    def append_breadcrumb(self, tenant_id, event_type, payload, conn=None):
        pass


class MockDbConn:
    def __init__(self):
        self.hitl_approvers = {
            "tenant-1": ["authorized-key-hex"]
        }
        self.requests = {
            "req-1": {
                "request_id": "req-1",
                "tenant_id": "tenant-1",
                "status": "pending",
                "workflow_id": "wf-1",
                "step_id": "step-1",
                "context_json": {"foo": "bar"},
                "context_bytes": b'{"foo":"bar"}',
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
                "signer_id": None,
                "signature": None,
                "decided_at": None,
            }
        }
        self.executed_queries = []

    def execute(self, query: str, params: tuple = ()):
        self.executed_queries.append((query, params))
        class MockCursor:
            def __init__(self, parent, q, p):
                self.parent = parent
                self.query = q
                self.params = p

            def fetchone(self):
                if "FOR UPDATE" in self.query:
                    req_id = self.params[0]
                    return self.parent.requests.get(req_id)
                elif "hitl_approvers" in self.query:
                    signer_id, tenant_id = self.params[0], self.params[1]
                    if signer_id in self.parent.hitl_approvers.get(tenant_id, []):
                        return {"public_key": signer_id}
                    return None
                return None
        return MockCursor(self, query, params)


class MockDbPool:
    def __init__(self):
        self.conn = MockDbConn()

    class MockContext:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self.conn

        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    def connection(self):
        return self.MockContext(self.conn)


def test_unauthorized_signature_gets_403(monkeypatch):
    # Disable UNSAFE_LOCAL_DEV to test production behavior
    monkeypatch.setenv("UNSAFE_LOCAL_DEV", "false")

    pool = MockDbPool()
    orch = MockOrchestrator()
    gc = MockGcrumbs()

    router = create_hitl_router(pool, orch, gc)
    app = FastAPI()
    app.include_router(router)

    client = TestClient(app)

    # Generate a random UNAUTHORIZED key
    priv = ed25519.Ed25519PrivateKey.generate()
    pub = priv.public_key()
    import cryptography.hazmat.primitives.serialization as serialization
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )
    pub_hex = pub_bytes.hex()

    # Sign the payload
    prefix = b"grafomem.hitl.approval.v1:"
    decision = b"approve"
    context_bytes = b'{"foo":"bar"}'
    signed_bytes = prefix + context_bytes + b"\x1f" + decision
    signature = priv.sign(signed_bytes)

    # Post attestation
    response = client.post(
        "/v1/hitl/requests/req-1/attest",
        json={
            "decision": "approve",
            "signer_id": pub_hex,
            "signature": signature.hex()
        }
    )

    # Assert 403 Forbidden
    assert response.status_code == 403
    assert response.json()["detail"] == "Signer not authorized"

    # Assert workflow was NOT resumed
    assert not orch.resumed
