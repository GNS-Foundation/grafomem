"""ADR-0020 (C4) — the receipt signer is not the erasure caller.

Non-vacuity: every guard here is shown to bite —
  * the saver's own key cannot produce a receipt the log accepts (forged local receipt fails),
  * a signer that is down refuses the delete BEFORE anything is erased,
  * a signer that dies AFTER the delete leaves a visible pending marker, drained by flush,
  * a tampered signer log fails verification.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from langgraph.checkpoint.memory import InMemorySaver

from grafomem_checkpoint import (
    GrafomemCheckpointSaver, GrafomemSerializer, RemoteReceiptSigner, LocalReceiptSigner,
    ReceiptPending, SignerUnavailable, verify_log, receipt_to_dict,
)
from grafomem.runtime import Receipt

TOKEN = "t0k3n-" + os.urandom(4).hex()


# ── a real ledger signer on a loopback port ──────────────────────────────────

class SignerProcess:
    """`grafomem signer` in-process: uvicorn on a free loopback port, start/stop at will."""

    def __init__(self, log_path: str):
        from aml.cloud.signer import LedgerSigner, ChainedJsonlLog, create_signer_app
        self.key = ed25519.Ed25519PrivateKey.generate()
        self.log_path = log_path
        self.signer = LedgerSigner(self.key, "ledger-signer-A", ChainedJsonlLog(log_path))
        self.app = create_signer_app(self.signer, TOKEN)
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = s.getsockname()[1]; s.close()
        self.url = f"http://127.0.0.1:{self.port}"
        self._server = None

    def start(self):
        import uvicorn
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self._server = uvicorn.Server(cfg)
        threading.Thread(target=self._server.run, daemon=True).start()
        for _ in range(100):
            if self._server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("signer did not start")

    def stop(self):
        if self._server:
            self._server.should_exit = True
            for _ in range(100):
                if not self._server.started:
                    break
                time.sleep(0.05)
            self._server = None
            time.sleep(0.1)


@pytest.fixture
def signer(tmp_path):
    sp = SignerProcess(str(tmp_path / "signer.jsonl"))
    sp.start()
    yield sp
    sp.stop()


def _saver(signer_url, tmp_path, **kw):
    priv = ed25519.Ed25519PrivateKey.generate()          # the CHECKPOINT key; not the receipt key
    serde = GrafomemSerializer(private_key=priv, key_id="checkpoint-key")
    inner = InMemorySaver(serde=serde)
    remote = RemoteReceiptSigner(signer_url, TOKEN, timeout=2.0, caller="test-saver")
    return GrafomemCheckpointSaver(inner, receipt_signer=remote,
                                   pending_path=str(tmp_path / "pending.jsonl"), **kw), priv


def _put(saver, thread_id):
    cfg = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    from langgraph.checkpoint.base import empty_checkpoint
    cp = empty_checkpoint(); cp["channel_values"] = {"x": f"secret of {thread_id}"}
    saver.put(cfg, cp, {"source": "input", "step": 0, "writes": {}, "parents": {}}, {})
    return cfg


# ── 1. custody separation: the saver holds no receipt key and no log handle ──

def test_receipt_is_signed_by_the_signer_not_the_saver(signer, tmp_path):
    saver, checkpoint_priv = _saver(signer.url, tmp_path)
    assert saver.receipt_signer.kind == "remote"
    assert not hasattr(saver.receipt_signer, "_priv") and not hasattr(saver.receipt_signer, "_log")

    _put(saver, "t1")
    saver.delete_thread("t1")
    r = saver.last_receipt("t1")
    assert r.key_id == "ledger-signer-A" != "checkpoint-key"
    assert r.verify(signer.key.public_key())                       # signer's key
    assert not r.verify(checkpoint_priv.public_key())              # NOT the saver's

    entries = saver.audit_log()                                    # read-only, through the signer
    assert len(entries) == 1 and entries[0]["caller"] == "test-saver"
    assert entries[0]["before_basis"] == "caller_asserted"         # honest: the signer cannot read the store
    ok, problems = verify_log(entries, saver.inner.serde.trusted_keys)   # the adapter's verifier, unchanged
    assert ok, problems

    # NON-VACUITY: a receipt the saver forges with its OWN key under the signer's key_id
    # verifies against nothing the auditor trusts, and a log entry built from it is rejected.
    ts = r.timestamp
    forged = Receipt(before=r.before, after=r.after, scope="t1", key_id="ledger-signer-A", timestamp=ts,
                     signature=checkpoint_priv.sign(f"{r.before}|{r.after}|t1|ledger-signer-A|{ts}".encode()))
    assert not forged.verify(signer.key.public_key())
    fake_entry = dict(entries[0]); fake_entry["receipt"] = receipt_to_dict(forged)
    ok, problems = verify_log([fake_entry], saver.inner.serde.trusted_keys)
    assert not ok and any("signature" in p or "tampered" in p for p in problems)


def test_signer_log_tamper_is_detected(signer, tmp_path):
    saver, _ = _saver(signer.url, tmp_path)
    for t in ("a", "b"):
        _put(saver, t); saver.delete_thread(t)
    assert signer.signer.verify() == (True, [])
    lines = open(signer.log_path).read().splitlines()
    e0 = json.loads(lines[0]); e0["receipt"]["after"] = "erased-but-not-really"
    with open(signer.log_path, "w") as fh:
        fh.write(json.dumps(e0) + "\n" + "\n".join(lines[1:]) + "\n")
    from aml.cloud.signer import ChainedJsonlLog, verify_entries
    ok, problems = verify_entries(ChainedJsonlLog(signer.log_path).entries(), signer.key.public_key())
    assert not ok and problems


# ── 2. fail closed: no signer, no erasure ───────────────────────────────────

def test_signer_down_refuses_the_delete_before_anything_is_erased(tmp_path):
    sp = SignerProcess(str(tmp_path / "s.jsonl"))          # never started
    saver, _ = _saver(sp.url, tmp_path)
    cfg = _put(saver, "keep-me")
    with pytest.raises(SignerUnavailable):
        saver.delete_thread("keep-me")
    assert saver.get_tuple(cfg) is not None                # still there: nothing was erased
    assert saver.pending() == []                           # and nothing is pending


# ── 3. signer dies AFTER the delete: pending marker, flush, visible gap ──────

def test_signer_failure_after_delete_is_pending_then_flushed(signer, tmp_path):
    saver, _ = _saver(signer.url, tmp_path)
    cfg = _put(saver, "gone")
    signer.stop()                                          # preflight will fail... so bypass it
    saver._signer.preflight = lambda: None                 # simulate: signer answered preflight, died before sign
    with pytest.raises(ReceiptPending):
        saver.delete_thread("gone")
    assert saver.get_tuple(cfg) is None                    # the erasure IS real
    pend = saver.pending()
    assert len(pend) == 1 and pend[0]["thread_id"] == "gone"
    assert saver.last_receipt("gone") is None              # no locally-forged stand-in

    signer.start()
    del saver._signer.preflight                            # real preflight again
    assert saver.flush_pending() == 1
    assert saver.pending() == []
    r = saver.last_receipt("gone")
    assert r is not None and r.verify(signer.key.public_key())
    e = saver.audit_log()[-1]
    assert e["caller_erased_at"] < e["recorded_at"]        # the gap is on the record


def test_allow_unreceipted_records_a_marker_instead_of_raising(signer, tmp_path):
    saver, _ = _saver(signer.url, tmp_path, allow_unreceipted=True)
    _put(saver, "u")
    signer.stop()
    saver._signer.preflight = lambda: None
    assert saver._record("u", "sha256:x", "erased") is None
    assert saver.pending()[0]["thread_id"] == "u"


# ── 4. auth ─────────────────────────────────────────────────────────────────

def test_wrong_token_is_refused(signer, tmp_path):
    bad = RemoteReceiptSigner(signer.url, "wrong", timeout=2.0)
    with pytest.raises(SignerUnavailable, match="401"):
        bad.preflight()


# ── 5. the local signer is the old behaviour, named ─────────────────────────

def test_local_signer_is_default_and_uses_the_checkpoint_key(tmp_path):
    priv = ed25519.Ed25519PrivateKey.generate()
    saver = GrafomemCheckpointSaver(InMemorySaver(serde=GrafomemSerializer(private_key=priv, key_id="k")),
                                    receipt_log=__import__("grafomem_checkpoint").InMemoryReceiptLog())
    assert isinstance(saver.receipt_signer, LocalReceiptSigner)
    _put(saver, "l"); saver.delete_thread("l")
    assert saver.last_receipt("l").key_id == "k" and saver.last_receipt("l").verify(priv.public_key())
    with pytest.raises(ValueError, match="not both"):
        GrafomemCheckpointSaver(InMemorySaver(serde=GrafomemSerializer(private_key=priv, key_id="k")),
                                receipt_log=__import__("grafomem_checkpoint").InMemoryReceiptLog(),
                                receipt_signer=LocalReceiptSigner(priv, "k"))
