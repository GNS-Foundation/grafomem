"""cgr.cosign.v1 conformance runner.

1. `test_corpus_wellformed` — ALWAYS runs (no verifier needed). Guards the corpus:
   structure, coverage of the vector families, that valid-expected records genuinely
   verify (both signatures + content digest), and the BYTE-LEVEL nesting invariant
   (N1/N2: the original system signature fails against the stripped/altered body) and
   the K2 lift (assertion digest ≠ recomputed body digest). This is what makes the
   corpus PR green before any cosign verifier exists.

2. `test_cosign_conformance` — runs each vector's `expect` against a verifier named by
   CGR_COSIGN_VERIFIER. SKIPS cleanly when none is wired (no cosign verifier exists
   yet — clients/cgr-verify does attestation only). The corpus is the executable target
   the future verifier must meet.

    CGR_COSIGN_VERIFIER=path/to/verify_bridge.py pytest tests/test_cosign_conformance.py -v
"""
import importlib
import importlib.util
import json
import os
import pathlib
import sys

import pytest

_CORPUS = pathlib.Path(__file__).resolve().parent.parent / "conformance" / "cgr-cosign-v1"
sys.path.insert(0, str(_CORPUS))
import generate as G  # noqa: E402  (reuse the exact production JCS + key helpers)

VECTORS = json.loads((_CORPUS / "vectors.json").read_text())["vectors"]
REGISTRY = json.loads((_CORPUS / "registry.json").read_text())
FAMILIES = {"W", "S", "P", "D", "R", "U", "N", "K", "A", "F", "X", "I"}


def _trusted_issuers(vec):
    """The trusted issuer set for a vector, derived from its `pinned_issuer` (decision 0011
    §8.2a). REQUIRED input to verify() with no default — this is the interface contract the
    corpus pins. The harness never reads the record's self-declared issuer_key_id for trust."""
    return ["ed25519:" + vec["pinned_issuer"]]


def _family(vid: str) -> str:
    return vid[0]


# ── Layer 1: corpus self-check (always runs) ─────────────────────────────────

def test_corpus_wellformed():
    ids = [v["id"] for v in VECTORS]
    assert len(ids) == len(set(ids)), "duplicate vector ids"

    # coverage: every family in the approved matrix is present
    assert {_family(i) for i in ids} >= FAMILIES, f"missing families: {FAMILIES - {_family(i) for i in ids}}"

    # registry fixture carries its loud in-file warnings and the pinned profiles
    assert "TEST FIXTURE" in REGISTRY["_WARNING"] and "UNWRITTEN" in REGISTRY["_WARNING"]
    # decision 0011: the trusted-issuer-set contract rides inside the fixture too
    assert "TRUSTED ISSUER SET" in REGISTRY["_WARNING_TRUSTED_ISSUERS"] \
        and "NO default" in REGISTRY["_WARNING_TRUSTED_ISSUERS"]
    assert set(REGISTRY["profiles"]) == {
        "test.bound.unconditional", "test.bound.predicate", "test.bound.predicate.in",
        "test.bound.predicate.in.malformed", "test.free"}

    for v in VECTORS:
        for k in ("id", "clause", "spec_lines", "title", "subject", "expect"):
            assert k in v, f"{v.get('id')}: missing {k}"
        exp = v["expect"]
        assert "valid" in exp, f"{v['id']}: expect.valid missing"
        if exp["valid"] is False:
            assert exp.get("reason_contains"), f"{v['id']}: reject vector needs reason_contains"

    # valid-expected records with a full approver block MUST genuinely verify — so a
    # future verifier's reject (if any) is attributable to the tested defect, not to
    # incidental malformation. The system signature is verified against the record's OWN
    # self-declared issuer_key_id (not a hardcoded 0x11) — the I2 control is a valid record
    # signed by 0x33, and it must verify under 0x33; its rejection-vs-acceptance is the
    # trusted-set's job (step 2a), not a crypto property.
    for v in VECTORS:
        rec = v["subject"]
        if v["expect"]["valid"] and rec.get("approver_signature") and rec.get("approval_assertion"):
            a = rec["approval_assertion"]
            issuer_pub = rec["system_metadata"]["issuer_key_id"].split(":", 1)[1]
            assert G._verify(G.APPROVER_PUB, G._sig_hex(rec["approver_signature"]),
                             G.DOMAIN_TAG + G._canon(a)), f"{v['id']}: approver sig should verify"
            assert G._verify(issuer_pub, G._sig_hex(rec["system_signature"]),
                             G._sysbody_bytes(rec)), f"{v['id']}: system sig should verify (self-declared issuer)"
            assert a["content_digest"] == G.content_digest(rec["content_body"]), \
                f"{v['id']}: content_digest should match"

    # THE NESTING INVARIANT, re-checked on the bytes from vectors.json (independent of
    # the generator's own proof): the original system signature must FAIL against the
    # stripped/altered body. This is the property, not "a signature recomputed over the
    # stripped body" (which would prove nothing).
    n1 = _subject("N1-stripped-approver-sig")
    assert "approver_signature" not in n1, "N1: approver signature must be stripped"
    assert not G._verify(G.ISSUER_PUB, G._sig_hex(n1["system_signature"]), G._sysbody_bytes(n1)), \
        "N1: original system signature MUST NOT verify against the stripped body (nesting)"

    n2 = _subject("N2-tampered-signed-field")
    assert not G._verify(G.ISSUER_PUB, G._sig_hex(n2["system_signature"]), G._sysbody_bytes(n2)), \
        "N2: original system signature MUST NOT verify against the tampered body"

    # K2 lift: the carried assertion's digest does not match the record's actual content.
    k2 = _subject("K2-lift-to-different-content")
    assert k2["approval_assertion"]["content_digest"] != G.content_digest(k2["content_body"]), \
        "K2: lifted assertion digest must not match the new content"

    # U1–U3 are the decision-0010 amended vectors: unresolvable predicate -> reject.
    for uid in ("U1-predicate-field-absent", "U2-predicate-field-nonscalar", "U3-predicate-field-null"):
        v = _vector(uid)
        assert "amended-0010" in v.get("tags", []), f"{uid}: must be tagged amended-0010"
        assert v["expect"]["valid"] is False and "predicate_unresolved" in v["expect"]["reason_contains"]

    # F2 is the §8.9 degrade position, tagged so it flips cleanly if MUST-resolve is chosen.
    assert "flips-if-8.9-must-resolve" in _vector("F2-free-unresolved-reference").get("tags", [])

    # DEFECT PRESENCE — each reject vector must actually EMBODY the clause it names, on
    # the bytes (no verifier). This is what makes the corpus a trustworthy target before
    # a verifier exists, and what makes the per-family non-vacuity probes bite.
    def mode_of(p): return REGISTRY["profiles"].get(p, {}).get("approver_signature"), REGISTRY["profiles"].get(p, {}).get("approval_mode")

    s = _subject("S1-unknown-schema");   assert s["schema"] != "cgr.cosign.v1"
    s = _subject("P1-unknown-profile");  assert s["profile"] not in REGISTRY["profiles"]
    s = _subject("P2-mode-mismatch");    assert s["approval_mode"] != REGISTRY["profiles"][s["profile"]]["approval_mode"]
    s = _subject("D1-content-digest-mismatch")
    assert s["approval_assertion"]["content_digest"] != G.content_digest(s["content_body"])
    s = _subject("R1-unconditional-required-absent")
    assert REGISTRY["profiles"][s["profile"]]["approver_signature"] == "REQUIRED" and "approver_signature" not in s
    s = _subject("R2-predicate-holds-absent")
    assert s["content_body"].get("risk_class") == "high" and "approver_signature" not in s
    s = _subject("R3-predicate-false-absent")
    assert s["content_body"].get("risk_class") == "low" and "approver_signature" not in s      # valid: predicate false
    s = _subject("R4-optional-but-invalid-sig")
    assert s.get("approver_signature") and not G._verify(
        G.APPROVER_PUB, G._sig_hex(s["approver_signature"]), G.DOMAIN_TAG + G._canon(s["approval_assertion"]))
    # U1–U3: the required_when field is unresolvable (absent / non-scalar / null), no approver sig
    assert "risk_class" not in _subject("U1-predicate-field-absent")["content_body"]
    assert not isinstance(_subject("U2-predicate-field-nonscalar")["content_body"]["risk_class"], (str, int, float, bool))
    assert _subject("U3-predicate-field-null")["content_body"]["risk_class"] is None
    s = _subject("K1-nonce-replay")
    assert [s["approval_assertion"]["approver_key_id"], s["approval_assertion"]["record_nonce"]] \
        in _vector("K1-nonce-replay")["ledger"]["seen"]
    s = _subject("A1-approve-with-draft-digest")
    assert s["approval_assertion"]["approver_act"] == "approve" and "agent_draft_digest" in s["approval_assertion"]
    s = _subject("A2-modify-without-draft-digest")
    assert s["approval_assertion"]["approver_act"] == "modify" and "agent_draft_digest" not in s["approval_assertion"]
    s = _subject("A3-override-valid")
    assert s["approval_assertion"]["approver_act"] == "override" and "agent_draft_digest" in s["approval_assertion"]
    s = _subject("F1-free-malformed-reference")
    assert any(not _is_b2_256(r) for r in s["content_body"]["references"])
    s = _subject("F2-free-unresolved-reference")
    assert all(_is_b2_256(r) for r in s["content_body"]["references"])   # well-formed, just unresolvable

    # ── decision 0011 additions ───────────────────────────────────────────────
    # coverage: the 0011 vectors are present and tagged
    for vid in ("U4-predicate-in-nonarray", "R5-predicate-in-holds", "R6-predicate-in-false",
                "I1-untrusted-issuer", "I2-trusted-issuer-control"):
        assert "amended-0011" in _vector(vid).get("tags", []), f"{vid}: must be tagged amended-0011"

    # GAP 2 defect presence — the `in` operator, well-formed and malformed:
    # U4: the profile's predicate uses `in` with a NON-ARRAY value (the malformed operand),
    # the record is high-risk and UNSIGNED (so a verifier reading non-array `in` as
    # predicate-false passes an unsigned high-risk record — the fail-open this vector catches).
    s = _subject("U4-predicate-in-nonarray")
    _pred = REGISTRY["profiles"][s["profile"]]["approver_signature"]["required_when"]
    assert _pred["op"] == "in" and not isinstance(_pred["value"], list), "U4: profile `in` value must be non-array"
    assert s["content_body"].get("risk_class") == "high" and "approver_signature" not in s, \
        "U4: must be a high-risk UNSIGNED record (fail-open is dangerous, not cosmetic)"
    assert _vector("U4-predicate-in-nonarray")["expect"]["reason_contains"] == "predicate_unresolved"
    # R5/R6: the WELL-FORMED `in` (array value), pinned in both directions.
    r5p = REGISTRY["profiles"][_subject("R5-predicate-in-holds")["profile"]]["approver_signature"]["required_when"]
    assert r5p["op"] == "in" and isinstance(r5p["value"], list), "R5: profile `in` value must be an array"
    assert _subject("R5-predicate-in-holds")["content_body"]["risk_class"] in r5p["value"] \
        and _subject("R5-predicate-in-holds").get("approver_signature"), "R5: member present + signed (required, holds)"
    assert _subject("R6-predicate-in-false")["content_body"]["risk_class"] not in r5p["value"] \
        and "approver_signature" not in _subject("R6-predicate-in-false"), "R6: member absent + unsigned (optional, false)"

    # GAP 1 defect presence + BYTE PROBE — issuer pinning (I1), mirroring the N1 invariant:
    # the record's system signature verifies CLEANLY under its own self-declared 0x33 issuer
    # (so a step-2a-skipping verifier passes it), while the vector pins 0x11 — the mismatch is
    # the defect. Without the clean self-verify the vector would prove nothing (it would reject
    # on crypto, not on trust).
    i1v = _vector("I1-untrusted-issuer")
    i1 = i1v["subject"]
    _i1_issuer = i1["system_metadata"]["issuer_key_id"].split(":", 1)[1]
    assert _i1_issuer == G.UNTRUSTED_ISSUER_PUB, "I1: record must self-declare the 0x33 issuer"
    assert i1v["pinned_issuer"] == G.ISSUER_PUB, "I1: the trusted set must pin 0x11 (0x33 untrusted)"
    assert _i1_issuer != i1v["pinned_issuer"], "I1: self-declared issuer must differ from the pinned/trusted one"
    assert G._verify(_i1_issuer, G._sig_hex(i1["system_signature"]), G._sysbody_bytes(i1)), \
        "I1: system signature MUST verify under its self-declared 0x33 key (otherwise-valid; a 2a-skip passes it)"
    assert not G._verify(i1v["pinned_issuer"], G._sig_hex(i1["system_signature"]), G._sysbody_bytes(i1)), \
        "I1: the 0x33 signature must NOT verify under the pinned 0x11 key"
    assert i1v["expect"]["reason_contains"] == "issuer_untrusted"
    # I2 positive control: SAME record, but pins 0x33 -> trust check passes -> valid.
    i2v = _vector("I2-trusted-issuer-control")
    assert i2v["subject"] == i1, "I2 must be byte-identical to I1 (only the trusted set differs)"
    assert i2v["pinned_issuer"] == G.UNTRUSTED_ISSUER_PUB and i2v["expect"]["valid"] is True, \
        "I2: control pins 0x33 and expects valid (proves I1 rejects on trust, not otherwise)"


def _is_b2_256(ref: str) -> bool:
    if not isinstance(ref, str) or not ref.startswith("b2-256:"):
        return False
    h = ref.split(":", 1)[1]
    return len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def _vector(vid):
    return next(v for v in VECTORS if v["id"] == vid)


def _subject(vid):
    return _vector(vid)["subject"]


# ── Layer 2: run vectors against a cosign verifier (skips when none is wired) ──

def _load_verifier():
    ref = os.environ.get("CGR_COSIGN_VERIFIER")
    if not ref:
        return None
    if ref.endswith(".py"):
        spec = importlib.util.spec_from_file_location("cosign_verifier", ref)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    return importlib.import_module(ref)


@pytest.mark.skipif(not os.environ.get("CGR_COSIGN_VERIFIER"),
                    reason="set CGR_COSIGN_VERIFIER=<module/path exposing verify()> to run the vectors")
@pytest.mark.parametrize("vec", VECTORS, ids=[v["id"] for v in VECTORS])
def test_cosign_conformance(vec):
    verifier = _load_verifier()
    if verifier is None:
        pytest.skip("no verifier")
    # decision 0011 §8.2a: verify() takes the trusted issuer set (REQUIRED, no default) as a
    # 4th argument, derived from the vector's pinned_issuer. The amended verifiers consume it;
    # the pre-0011 verifiers (3-arg) are updated in the verifiers PR that follows this corpus.
    result = verifier.verify(vec["subject"], REGISTRY, vec.get("ledger", {}), _trusted_issuers(vec))
    exp = vec["expect"]
    assert result.get("valid") == exp["valid"], f"{vec['id']}: {result}"
    if exp["valid"] is False:
        assert exp["reason_contains"] in (result.get("reason") or ""), f"{vec['id']}: {result}"


if __name__ == "__main__":
    test_corpus_wellformed()
    print("corpus self-check passed")
