"""ADR-0020 — the ledger signer service (`grafomem signer`)."""
import json
import os

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient

from aml.cloud.signer import (
    ChainedJsonlLog, LedgerSigner, create_signer_app, verify_entries, entry_hash, GENESIS_HASH,
)

H = {"Authorization": "Bearer tok"}


@pytest.fixture
def svc(tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    s = LedgerSigner(key, "signer-k", ChainedJsonlLog(str(tmp_path / "log.jsonl")))
    return s, TestClient(create_signer_app(s, "tok")), key


def test_requires_a_token_unless_unsafe_local_dev(tmp_path, monkeypatch):
    s = LedgerSigner(ed25519.Ed25519PrivateKey.generate(), "k", ChainedJsonlLog(str(tmp_path / "l")))
    monkeypatch.delenv("UNSAFE_LOCAL_DEV", raising=False)
    with pytest.raises(RuntimeError, match="GRAFOMEM_SIGNER_TOKEN"):
        create_signer_app(s, None)
    monkeypatch.setenv("UNSAFE_LOCAL_DEV", "1")
    create_signer_app(s, None)


def test_auth_chain_and_verify(svc):
    s, c, key = svc
    assert c.get("/v1/sign/health").status_code == 401
    assert c.post("/v1/sign/receipt", headers={"Authorization": "Bearer nope"},
                  json={"before": "b", "after": "a", "scope": "t"}).status_code == 401
    r1 = c.post("/v1/sign/receipt", headers=H, json={"before": "sha256:1", "after": "erased", "scope": "t1",
                                                     "caller": "api", "caller_erased_at": "2026-11-10T00:00:00+00:00"}).json()
    r2 = c.post("/v1/sign/receipt", headers=H, json={"before": "sha256:2", "after": "key_destroyed:ab", "scope": "t2"}).json()
    assert r1["entry"]["seq"] == 0 and r1["entry"]["prev_entry_hash"] == GENESIS_HASH
    assert r2["entry"]["prev_entry_hash"] == r1["entry"]["entry_hash"]
    assert r1["entry"]["before_basis"] == "caller_asserted" and r1["entry"]["caller"] == "api"
    assert r1["receipt"]["key_id"] == "signer-k"
    key.public_key().verify(bytes.fromhex(r1["receipt"]["signature"]),
                            f"sha256:1|erased|t1|signer-k|{r1['receipt']['timestamp']}".encode())
    v = c.get("/v1/sign/verify", headers=H).json()
    assert v == {"ok": True, "problems": [], "entries": 2}
    assert c.post("/v1/sign/receipt", headers=H, json={"before": "", "after": "a", "scope": "t"}).status_code == 422


def test_restart_continues_the_chain_and_tamper_is_detected(tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    path = str(tmp_path / "log.jsonl")
    LedgerSigner(key, "k", ChainedJsonlLog(path)).sign_receipt(before="b1", after="a", scope="s")
    s2 = LedgerSigner(key, "k", ChainedJsonlLog(path))               # restart
    e = s2.sign_receipt(before="b2", after="a", scope="s")["entry"]
    assert e["seq"] == 1 and s2.verify() == (True, [])
    lines = open(path).read().splitlines()
    e0 = json.loads(lines[0]); e0["receipt"]["after"] = "not-erased"
    open(path, "w").write(json.dumps(e0) + "\n" + lines[1] + "\n")
    ok, problems = verify_entries(ChainedJsonlLog(path).entries(), key.public_key())
    assert not ok and any("tampered" in p for p in problems) and any("signature" in p for p in problems)
    # a different key must not verify an honest log either
    ok, _ = verify_entries(ChainedJsonlLog(str(tmp_path / "x")).entries() or
                           [LedgerSigner(key, "k", ChainedJsonlLog(str(tmp_path / "y"))).sign_receipt(before="b", after="a", scope="s")["entry"]],
                           ed25519.Ed25519PrivateKey.generate().public_key())
    assert not ok


def test_entry_format_matches_the_langgraph_adapters_verify_log(svc):
    """The adapter's verifier is the authority for receipt logs; the signer's entries must
    verify under it unchanged."""
    pytest.importorskip("grafomem_checkpoint")
    from grafomem_checkpoint import verify_log
    s, c, key = svc
    c.post("/v1/sign/receipt", headers=H, json={"before": "b", "after": "erased", "scope": "t"})
    entries = c.get("/v1/sign/entries", headers=H).json()
    ok, problems = verify_log(entries, {"signer-k": key.public_key()})
    assert ok, problems
    assert entry_hash(entries[0]["prev_entry_hash"], entries[0]["receipt"]) == entries[0]["entry_hash"]
