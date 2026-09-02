#!/usr/bin/env python3
"""
tests/test_v4_conformance.py — the cgr.attestation.v4 conformance corpus runner.

Two layers:

1. `test_corpus_wellformed` — ALWAYS runs (no verifier needed). Guards the corpus
   itself against rot/regeneration drift: structure, signatures verify against the
   pinned test issuer (T9 against the agent key), the T2≠T8 distinct-`lineage_status`
   invariant (the #85 amendment), that every vector declares a mode, and that both
   enforcement modes (0006 enforce-or-label) are exercised.

2. `test_v4_conformance[<vector>]` — runs each vector against a v4 verifier, and
   SKIPS cleanly when none is wired (like test_v3_conformance.py skips without
   GRAFOMEM_DB_URL). Wire a verifier by setting CGR_V4_VERIFIER to an importable
   module/path exposing `verify(subject, ledger, pinned_issuer_hex, held_edges, mode,
   seek_fails) -> {valid, reason?, lineage_status?, superseded?, ...}`. Without it this
   layer skips — the corpus is the executable target the verifier must meet.

    pytest tests/test_v4_conformance.py -v
    CGR_V4_VERIFIER=aml.cgr.v4_verify pytest tests/test_v4_conformance.py -v
"""
import os, json, importlib, pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CORPUS = _ROOT / "conformance" / "cgr-attestation-v4" / "vectors.json"

import pytest

def _load():
    return json.loads(_CORPUS.read_text())


def _canonical_body(att):
    """rfc8785 (JCS) bytes of the signed body — byte-identical to aml.cgr.attestation.canonical_body,
    inlined so the runner does not depend on which `aml` is installed in the env. Only `signature`
    and `evidence_ref` are excluded; `relates_to` is part of the signed body (§2.4), so an attestation
    carrying an *unsigned* `relates_to` (B1) fails signature verification here — which is the point."""
    import rfc8785
    body = {k: v for k, v in att.items() if k not in ("signature", "evidence_ref")}
    return rfc8785.dumps(body)


def _sig_ok(att, pub_hex):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    try:
        pub = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))
        pub.verify(bytes.fromhex(att["signature"]), _canonical_body(att))
        return True
    except Exception:
        return False


# ── Layer 1: corpus self-check (always runs) ─────────────────────────────────

def test_corpus_wellformed():
    d = _load()
    vs = d["vectors"]
    assert vs, "empty corpus"
    byid = {v["id"]: v for v in vs}
    assert len(byid) == len(vs), "duplicate vector ids"

    for v in vs:
        for k in ("id", "clause", "spec_lines", "title", "mode", "subject", "ledger",
                  "held_edges", "pinned_issuer", "expect"):
            assert k in v, f"{v.get('id')}: missing {k}"
        assert isinstance(v["held_edges"], list), f"{v['id']}: held_edges must be a list"
        assert v["mode"] in ("enforcing", "non-enforcing"), f"{v['id']}: mode must be enforcing/non-enforcing"
        assert v["expect"] is not None and "valid" in v["expect"], f"{v['id']}: verdict required"

    # Signatures. Two vectors are DESIGNED not to verify against the pinned issuer:
    #   T9 — agent-signed continues (wrong signer);
    #   B1 — relates_to present but not covered by the signature (unsigned edge).
    # Every other subject must verify against the pinned Foundation issuer.
    issuer, agent = d["issuer_pubkey_hex"], d["agent_pubkey_hex"]
    for v in vs:
        s = v["subject"]
        if v["id"].startswith("T9"):
            assert not _sig_ok(s, issuer) and _sig_ok(s, agent), "T9 must be agent-signed, not issuer-signed"
        elif v["id"].startswith("B1"):
            assert not _sig_ok(s, issuer), "B1 (unsigned relates_to) must fail issuer signature verification"
        else:
            assert _sig_ok(s, issuer), f"{v['id']}: subject must verify against the pinned issuer"
        # held-edge records are Foundation-issued and must verify against the pinned issuer.
        for he in v["held_edges"]:
            assert _sig_ok(he, issuer), f"{v['id']}: held edge must verify against the pinned issuer"

    # THE #85 GUARD: a continues cycle and an unreachable predecessor are DISTINCT signals.
    t2 = byid["T2-continues-cycle"]["expect"]["lineage_status"]
    t8 = byid["T8-continues-unreachable"]["expect"]["lineage_status"]
    assert t2 == "anomaly_cycle" and t8 == "truncated_unavailable" and t2 != t8, \
        "T2 (cycle) and T8 (unreachable) MUST assert distinct lineage_status — see #85"

    assert d["vector_count"] == len(vs)
    # both enforcement modes are exercised by the corpus (0006 enforce-or-label)
    assert {"enforcing", "non-enforcing"} <= {v["mode"] for v in vs}


# ── Layer 2: run vectors against a v4 verifier (skips when none is wired) ─────

def _verifier():
    # CGR_V4_VERIFIER may be an importable module name OR a path to a .py file
    # (e.g. the conformance bridge at conformance/cgr-attestation-v4/verify_bridge.py).
    mod = os.environ.get("CGR_V4_VERIFIER")
    if not mod:
        return None
    try:
        if mod.endswith(".py") or os.sep in mod:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_cgr_v4_verifier", mod)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m
        return importlib.import_module(mod)
    except Exception as e:  # pragma: no cover
        pytest.fail(f"CGR_V4_VERIFIER={mod} not loadable: {e}")


_ALL = _load()["vectors"]


@pytest.mark.skipif(not os.environ.get("CGR_V4_VERIFIER"),
                    reason="set CGR_V4_VERIFIER=<module with verify()> to run the v4 conformance vectors")
@pytest.mark.parametrize("vec", _ALL, ids=[v["id"] for v in _ALL])
def test_v4_conformance(vec):
    verify = _verifier().verify
    res = verify(vec["subject"], vec["ledger"], vec["pinned_issuer"], vec["held_edges"],
                 vec["mode"], vec.get("seek_fails", False))
    exp = vec["expect"]
    assert res.get("valid") == exp["valid"], f"{vec['id']}: valid mismatch — {res}"
    if "lineage_status" in exp:
        assert res.get("lineage_status") == exp["lineage_status"], \
            f"{vec['id']}: lineage_status mismatch — {res}"
    if "superseded" in exp:
        assert res.get("superseded") == exp["superseded"], \
            f"{vec['id']}: superseded mismatch — {res}"
    if "evidence_tier" in exp:
        assert res.get("evidence_tier") == exp["evidence_tier"], \
            f"{vec['id']}: evidence_tier mismatch — {res}"
    if "reason_contains" in exp and not exp["valid"]:
        assert exp["reason_contains"] in (res.get("reason") or ""), \
            f"{vec['id']}: reason should contain '{exp['reason_contains']}' — {res}"


if __name__ == "__main__":
    test_corpus_wellformed()
    d = _load()
    enf = sum(1 for v in d["vectors"] if v["mode"] == "enforcing")
    non = sum(1 for v in d["vectors"] if v["mode"] == "non-enforcing")
    print(f"corpus OK: {d['vector_count']} vectors ({enf} enforcing, {non} non-enforcing)")
