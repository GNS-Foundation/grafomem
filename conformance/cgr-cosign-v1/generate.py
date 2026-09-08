#!/usr/bin/env python3
"""
Generator for the cgr.cosign.v1 conformance corpus.

Produces `vectors.json` (+ reads `registry.json`, writes `issuer.json`): signed test
vectors that encode the NORMATIVE MUST rules of docs/cgr/cgr-cosign-v1-spec.md, as
amended by decision 0010 (predicate-unresolved -> REJECT). A cosign verifier is
conformant iff it returns each vector's `expect` verdict.

Deterministic: fixed TEST keypairs (repeating-byte seeds — NOT real keys), so
re-running yields byte-identical output. Reuses the production JCS canonicalizer
(`rfc8785`), Ed25519-over-JCS with **no prehash**, BLAKE2b-256 content digests, and the
real DOMAIN_TAG — so vector bytes match a real verifier's.

    python3 conformance/cgr-cosign-v1/generate.py     # writes vectors.json + issuer.json

PROFILE REGISTRY: profile resolution (§8 step 2) is normative, but the registry
DOCUMENT is unwritten (§11 Q3). `registry.json` here is a TEST FIXTURE the corpus went
first on — the real registry must match these entries or every vector retargets. See
README.md (boxed warning) and the header comment in registry.json.
"""
from __future__ import annotations
import sys, os, json, hashlib, pathlib

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_ROOT / "src"))

import rfc8785  # noqa: E402  (production JCS canonicalizer)
from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402

# ── deterministic TEST keys (repeating-byte seeds; NOT real keys) ─────────────
ISSUER_SK = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([0x11]) * 32)
APPROVER_SK = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([0x22]) * 32)
# 0x33 — a THIRD test key: a well-formed issuer that is NOT in the trusted set (decision 0011,
# gap 1). Records it signs verify cleanly under their own self-declared issuer_key_id — so a
# verifier that skips §8 step 2a (issuer pinning) passes them. That is the defect I1 isolates.
UNTRUSTED_ISSUER_SK = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([0x33]) * 32)
def _pub_hex(sk): return sk.public_key().public_bytes_raw().hex()
ISSUER_PUB = _pub_hex(ISSUER_SK)
APPROVER_PUB = _pub_hex(APPROVER_SK)
UNTRUSTED_ISSUER_PUB = _pub_hex(UNTRUSTED_ISSUER_SK)

SCHEMA = "cgr.cosign.v1"
DOMAIN_TAG = b"grafomem.hitl.approval.v1:"          # §9 — the real signer's domain tag
APPROVER_KEY_ID = "ed25519:" + APPROVER_PUB
ISSUER_KEY_ID = "ed25519:" + ISSUER_PUB
UNTRUSTED_ISSUER_KEY_ID = "ed25519:" + UNTRUSTED_ISSUER_PUB
ENVELOPE_KEYS = ("system_signature", "evidence_ref")  # §2.3 — excluded from the system's signed body


def _canon(obj) -> bytes:
    return rfc8785.dumps(obj)


def content_digest(body) -> str:
    """§2.1 — 'b2-256:' ‖ hex(BLAKE2b-256(JCS(content_body))), lowercase, 64 hex."""
    return "b2-256:" + hashlib.blake2b(_canon(body), digest_size=32).hexdigest()


def assertion(content_body, *, approver_act="approve", record_nonce="nonce-0001",
              agent_draft_digest=None, decision_date="2026-09-08T00:00:00Z",
              approver_id="did:person:test-approver", digest_override=None, **overrides):
    """Build an approval_assertion (§1 Layer 2). `digest_override` forges the bound
    content_digest (for the lift/mismatch vectors)."""
    a = {
        "content_digest": digest_override or content_digest(content_body),
        "approver_id": approver_id,
        "approver_key_id": APPROVER_KEY_ID,
        "approver_act": approver_act,
        "decision_date": decision_date,
        "record_nonce": record_nonce,
    }
    if agent_draft_digest is not None:
        a["agent_draft_digest"] = agent_draft_digest
    a.update(overrides)
    return a


def approver_sign(a) -> str:
    """§2.2 — Ed25519 over DOMAIN_TAG ‖ JCS(approval_assertion), no prehash."""
    return "ed25519-sig:" + APPROVER_SK.sign(DOMAIN_TAG + _canon(a)).hex()


def system_sign(record, sk=ISSUER_SK) -> str:
    """§2.3 — Ed25519 over JCS(record minus envelope keys), which INCLUDES approver_signature.
    `sk` selects the signing key (0x33 for the untrusted-issuer vectors, decision 0011)."""
    body = {k: v for k, v in record.items() if k not in ENVELOPE_KEYS}
    return "ed25519-sig:" + sk.sign(_canon(body)).hex()


def record(profile, approval_mode, content_body, *, a=None, approver_sig=None,
           include_approver=True, sign_system=True,
           issuer_sk=ISSUER_SK, issuer_key_id=ISSUER_KEY_ID):
    """Assemble a cosign record. `a` is the approval_assertion (omit for approver-less
    vectors); `approver_sig` overrides the computed signature (for invalid-sig vectors).
    `include_approver=False` omits assertion+signature entirely (approver-less).
    `issuer_sk`/`issuer_key_id` select the system issuer — kept consistent so the record
    ALWAYS self-verifies under its own self-declared `issuer_key_id` (the point of I1: it
    is cryptographically valid; only the trust check rejects it)."""
    rec = {"schema": SCHEMA, "profile": profile, "approval_mode": approval_mode,
           "content_body": content_body}
    if include_approver and a is not None:
        rec["approval_assertion"] = a
        rec["approver_signature"] = approver_sig if approver_sig is not None else approver_sign(a)
    rec["system_metadata"] = {"issuer": "gns-foundation", "issuer_key_id": issuer_key_id,
                              "recorded_at": "2026-09-08"}
    if sign_system:
        rec["system_signature"] = system_sign(rec, issuer_sk)
    rec["evidence_ref"] = None
    return rec


VECTORS = []
def V(id, clause, lines, title, subject, expect, *, ledger=None, tags=None, pinned_issuer=None):
    """`pinned_issuer` is the hex pubkey of the SOLE trusted issuer for this vector; the
    harness derives the trusted set from it (decision 0011 §8.2a). Defaults to 0x11 — so
    every pre-existing vector keeps its verdict unchanged under the amended interface
    (the regression property). Only I1/I2 override it."""
    entry = {"id": id, "clause": clause, "spec_lines": lines, "title": title,
             "pinned_issuer": pinned_issuer or ISSUER_PUB, "approver_pub": APPROVER_PUB,
             "subject": subject, "expect": expect}
    if ledger is not None:
        entry["ledger"] = ledger
    if tags:
        entry["tags"] = tags
    VECTORS.append(entry)


# profiles the corpus pins (must match registry.json)
P_UNCOND = "test.bound.unconditional"
P_PRED = "test.bound.predicate"
P_PRED_IN = "test.bound.predicate.in"                # §5.1 `in` operator, WELL-FORMED array value
P_PRED_IN_BAD = "test.bound.predicate.in.malformed"  # §5.1 `in` with a NON-ARRAY value (0011 gap 2)
P_FREE = "test.free"

# a plain content body for a bound decision
DECISION = {"decision": "file", "risk_class": "high", "case_id": "c-1"}
DECISION_LOW = {"decision": "file", "risk_class": "low", "case_id": "c-2"}
FREE_BODY = {"kind": "learning-tx", "references": ["b2-256:" + "ab" * 32, "b2-256:" + "cd" * 32]}
B2 = lambda tag: "b2-256:" + (tag * 32)[:64]


# ── W: valid baselines ────────────────────────────────────────────────────────
V("W1-valid-bound", "§1/§8", "51-87",
  "valid bound record, approve, two nested signatures -> valid",
  record(P_UNCOND, "bound", DECISION, a=assertion(DECISION)),
  {"valid": True, "surfaced": {"approver_act": "approve"}})

V("W2-valid-free", "§5", "187-205",
  "valid free record: content_body references records by b2-256 hash, approver present -> valid",
  record(P_FREE, "free", FREE_BODY, a=assertion(FREE_BODY)),
  {"valid": True})

# ── S / P: schema + profile resolution (§8 steps 1-2) ─────────────────────────
V("S1-unknown-schema", "§8.1", "316",
  "schema != cgr.cosign.v1 -> reject (out of scope)",
  record(P_UNCOND, "bound", DECISION, a=assertion(DECISION)) | {"schema": "cgr.cosign.v2"},
  {"valid": False, "reason_contains": "schema"})

V("P1-unknown-profile", "§8.2", "317-319",
  "profile absent from the registry -> reject (fail closed)",
  record("test.not.registered", "bound", DECISION, a=assertion(DECISION)),
  {"valid": False, "reason_contains": "profile"})

V("P2-mode-mismatch", "§8.2", "317-319",
  "record.approval_mode != profile's declared mode -> reject",
  record(P_UNCOND, "free", DECISION, a=assertion(DECISION)),   # profile declares bound
  {"valid": False, "reason_contains": "mode"})

# ── D: content integrity, and ORDERING (step 3 before 3a) ─────────────────────
# predicate WOULD hold (risk_class=high) AND approver present, but the digest is stale:
# a conformant verifier rejects on content integrity (step 3) before evaluating the predicate.
_d1 = record(P_PRED, "bound", DECISION, a=assertion(DECISION, digest_override="b2-256:" + "00" * 32))
V("D1-content-digest-mismatch", "§8.3", "320-322",
  "content_digest != BLAKE2b-256(JCS(content_body)) -> reject (before predicate eval)",
  _d1, {"valid": False, "reason_contains": "content_digest"})

# ── R: required-ness (§5.1 / §8.3a / §8.4) ────────────────────────────────────
V("R1-unconditional-required-absent", "§8.4", "336-337",
  "profile requires approver signature (unconditional), none present -> reject",
  record(P_UNCOND, "bound", DECISION, include_approver=False),
  {"valid": False, "reason_contains": "approver"})

V("R2-predicate-holds-absent", "§5.1/§8.4", "233-238",
  "predicate holds (risk_class=high), approver signature absent -> reject",
  record(P_PRED, "bound", DECISION, include_approver=False),
  {"valid": False, "reason_contains": "approver"})

V("R3-predicate-false-absent", "§5.1/§8.3a", "233-238",
  "predicate resolves false (risk_class=low), approver signature absent -> PASS (optional)",
  record(P_PRED, "bound", DECISION_LOW, include_approver=False),
  {"valid": True})

# predicate false (optional) but a present approver signature is INVALID -> must still verify (§8.4)
_r4 = record(P_PRED, "bound", DECISION_LOW,
             a=assertion(DECISION_LOW), approver_sig="ed25519-sig:" + "00" * 64)
V("R4-optional-but-invalid-sig", "§8.4", "336-339",
  "approver signature present (optional) but invalid -> reject (present MUST verify)",
  _r4, {"valid": False, "reason_contains": "approver"})

# ── U: unresolvable predicate -> REJECT (decision 0010) ───────────────────────
V("U1-predicate-field-absent", "§5.1/§8.3a", "225-228",
  "required_when.field absent from content_body -> reject predicate_unresolved (0010)",
  record(P_PRED, "bound", {"decision": "file", "case_id": "c-3"}, include_approver=False),
  {"valid": False, "reason_contains": "predicate_unresolved"},
  tags=["amended-0010"])

V("U2-predicate-field-nonscalar", "§5.1/§8.3a", "225-228",
  "required_when.field resolves to a non-scalar (object) -> reject predicate_unresolved (0010)",
  record(P_PRED, "bound", {"decision": "file", "risk_class": {"nested": True}, "case_id": "c-4"},
         include_approver=False),
  {"valid": False, "reason_contains": "predicate_unresolved"},
  tags=["amended-0010"])

V("U3-predicate-field-null", "§5.1/§8.3a", "225-228",
  "required_when.field is null -> reject predicate_unresolved (0010)",
  record(P_PRED, "bound", {"decision": "file", "risk_class": None, "case_id": "c-5"},
         include_approver=False),
  {"valid": False, "reason_contains": "predicate_unresolved"},
  tags=["amended-0010"])

# U4: the `in` operator with a NON-ARRAY `value` (decision 0011, gap 2). A verifier that
# reads a non-array `in` as predicate-FALSE treats this high-risk record as approval-OPTIONAL
# and PASSES it unsigned — the exact fail-open class 0010 closed, one operator over. A
# conformant verifier rejects: a scalar operand cannot be tested for membership -> UNDETERMINED.
# The record is UNSIGNED so the fail-open is dangerous (not merely a wording difference).
V("U4-predicate-in-nonarray", "§5.1/§8.3a", "255-262",
  "`in` operator with a non-array value -> reject predicate_unresolved (0011: unresolvable is uniform)",
  record(P_PRED_IN_BAD, "bound", DECISION, include_approver=False),   # risk_class=high, unsigned
  {"valid": False, "reason_contains": "predicate_unresolved"},
  tags=["amended-0011"])

# ── R5/R6: the WELL-FORMED `in` operator, pinned in BOTH directions (0011, gap 2) ──────
# R5: `in` holds (member present) -> approver REQUIRED -> signed record -> valid.
V("R5-predicate-in-holds", "§5.1/§8.4", "255-256",
  "`in` predicate holds (risk_class in [high,critical]), approver present -> valid",
  record(P_PRED_IN, "bound", DECISION, a=assertion(DECISION)),        # risk_class=high, signed
  {"valid": True},
  tags=["amended-0011"])

# R6: `in` false (member absent) -> approver OPTIONAL -> unsigned record -> valid.
V("R6-predicate-in-false", "§5.1/§8.3a", "255-256",
  "`in` predicate false (risk_class not in [high,critical]), approver absent -> PASS (optional)",
  record(P_PRED_IN, "bound", DECISION_LOW, include_approver=False),   # risk_class=low, unsigned
  {"valid": True},
  tags=["amended-0011"])

# ── N: nesting / system-signature coverage (§7.1, §8.6) ───────────────────────
# CORPUS CORRECTION (found implementing the JS verifier, v4-style): the original N1/N2
# used a REQUIRED profile and rejected at §8.4 (approver required-and-absent / present-and-
# invalid) BEFORE reaching the system-signature check (§8.6) — so they did NOT isolate the
# nesting property. A non-conformant PARALLEL-signature verifier would also reject them.
# Fixed to force the rejection through §8.6.
#
# N1: strip approver_signature under an OPTIONAL profile (predicate false -> approver
# optional), keeping the original system signature. §8.4 passes (optional+absent); the
# rejection MUST come from §8.6, because the system signature covered a body that INCLUDED
# approver_signature. This ISOLATES nesting: a parallel-signature verifier accepts the
# stripped record; a nested one rejects.
_valid_opt = record(P_PRED, "bound", DECISION_LOW, a=assertion(DECISION_LOW))  # predicate false -> optional
_stripped = {k: v for k, v in _valid_opt.items() if k != "approver_signature"}
V("N1-stripped-approver-sig", "§7.1/§8.6", "289-291",
  "approver_signature stripped under an OPTIONAL profile -> system signature fails (nesting isolated)",
  _stripped, {"valid": False, "reason_contains": "system signature"})

# N2: tamper a signed-but-non-content field (system_metadata.recorded_at) after signing.
# content_digest (§8.3) still matches, so the rejection MUST come from §8.6 — proving the
# system signature covers the whole body (the substrate that makes nesting work). Altering
# the APPROVER signature instead cannot reach §8.6 (§8.4 catches a present-but-invalid sig
# first — that is R4), so N2 tests the system signature's body coverage rather than a
# masked approver-sig alteration.
_tampered = json.loads(json.dumps(_valid_opt))                 # deep copy
_tampered["system_metadata"]["recorded_at"] = "2099-01-01"     # signed field, changed post-signing
V("N2-tampered-signed-field", "§7.1/§8.6", "117-131",
  "a signed field altered after signing (content_digest still matches) -> system signature fails",
  _tampered, {"valid": False, "reason_contains": "system signature"})

# ── K: replay (§4 / §8.7) ─────────────────────────────────────────────────────
# K1: identical-content replay — a prior record with the same (approver_key_id, record_nonce)
# is in the ledger's `seen`; the later record MUST reject on nonce uniqueness.
_k1 = record(P_UNCOND, "bound", DECISION, a=assertion(DECISION, record_nonce="dup-nonce"))
V("K1-nonce-replay", "§4/§8.7", "164-179",
  "duplicate (approver_key_id, record_nonce) seen before -> reject (replay)",
  _k1, {"valid": False, "reason_contains": "nonce"},
  ledger={"seen": [[APPROVER_KEY_ID, "dup-nonce"]]})

# K2: different-content lift — take a valid assertion+approver_signature over content A and
# place it on a record whose content_body is B. The assertion's content_digest is A's; the
# recompute over B differs -> content integrity (step 3) rejects. The approval cannot be
# moved onto content it did not sign (§4 'different content' defense).
_a_over_A = assertion(DECISION, record_nonce="lift-nonce")
_sig_over_A = approver_sign(_a_over_A)
_k2 = record(P_UNCOND, "bound", DECISION_LOW, a=_a_over_A, approver_sig=_sig_over_A)  # body B, assertion over A
V("K2-lift-to-different-content", "§4/§8.3", "176-179",
  "valid approver signature lifted onto different content -> content_digest mismatch -> reject",
  _k2, {"valid": False, "reason_contains": "content_digest"})

# ── A: approver_act / agent_draft_digest consistency (§6 / §8.8) ──────────────
V("A1-approve-with-draft-digest", "§6/§8.8", "258-260",
  "approver_act=approve with agent_draft_digest present -> reject (must be absent)",
  record(P_UNCOND, "bound", DECISION,
         a=assertion(DECISION, approver_act="approve", agent_draft_digest=B2("ab"))),
  {"valid": False, "reason_contains": "agent_draft_digest"})

V("A2-modify-without-draft-digest", "§6/§8.8", "261-262",
  "approver_act=modify with agent_draft_digest absent -> reject (must be present)",
  record(P_UNCOND, "bound", DECISION, a=assertion(DECISION, approver_act="modify")),
  {"valid": False, "reason_contains": "agent_draft_digest"})

V("A3-override-valid", "§6/§8.8", "263-266",
  "approver_act=override with agent_draft_digest present -> valid (act surfaced)",
  record(P_UNCOND, "bound", DECISION,
         a=assertion(DECISION, approver_act="override", agent_draft_digest=B2("ef"))),
  {"valid": True, "surfaced": {"approver_act": "override"}})

# ── F: free-mode references (§8.9) ────────────────────────────────────────────
V("F1-free-malformed-reference", "§8.9", "341-345",
  "free mode with a malformed reference hash -> reject (references MUST be well-formed)",
  record(P_FREE, "free", {"kind": "learning-tx", "references": ["NOT-A-HASH"]},
         a=assertion({"kind": "learning-tx", "references": ["NOT-A-HASH"]})),
  {"valid": False, "reason_contains": "reference"})

# F2: free mode, well-formed reference NOT resolvable -> DEGRADE (surface, not reject).
# TAGGED: flips to reject if §8.9 [OPEN] resolves to MUST-resolve.
_f2body = {"kind": "learning-tx", "references": [B2("de")]}   # valid hex, not in any ledger
V("F2-free-unresolved-reference", "§8.9", "341-345",
  "free mode, well-formed but unresolvable reference -> degrade (surface references_unresolved, valid)",
  record(P_FREE, "free", _f2body, a=assertion(_f2body)),
  {"valid": True, "references_unresolved": True},
  ledger={"resolvable": []},
  tags=["flips-if-8.9-must-resolve"])

# ── X: surface-not-gate (§8 'MUST surface, MUST NOT gate') ────────────────────
V("X1-surface-approver-metadata", "§8", "347-353",
  "verifier MUST surface approver_id/approver_act/decision_date and MUST NOT gate on them -> valid + surfaced",
  record(P_UNCOND, "bound", DECISION,
         a=assertion(DECISION, approver_act="override", agent_draft_digest=B2("11"))),
  {"valid": True, "surfaced": {"approver_act": "override", "approver_id": "did:person:test-approver"}})

# ── I: issuer key pinning (§8 step 2a, decision 0011 gap 1) ────────────────────
# I1 and I2 are the SAME record — a well-formed cosign record whose system signature is
# produced by the 0x33 key and whose self-declared issuer_key_id is 0x33's. It verifies
# CLEANLY under its own issuer_key_id, so a verifier that skips step 2a (issuer pinning)
# passes it. The ONLY difference is the trusted set the harness derives from `pinned_issuer`:
#   I1 pins 0x11  -> 0x33 is NOT trusted -> reject issuer_untrusted   (the gap)
#   I2 pins 0x33  -> 0x33 IS  trusted    -> valid                     (positive control)
# The control proves I1 rejects SPECIFICALLY on the trust check, not on any other defect —
# without it, a vector that rejected for an unrelated reason would masquerade as testing 2a.
_untrusted_rec = record(P_UNCOND, "bound", DECISION, a=assertion(DECISION),
                        issuer_sk=UNTRUSTED_ISSUER_SK, issuer_key_id=UNTRUSTED_ISSUER_KEY_ID)
V("I1-untrusted-issuer", "§8.2a", "355-375",
  "system signature valid under a self-declared issuer NOT in the trusted set -> reject issuer_untrusted",
  _untrusted_rec, {"valid": False, "reason_contains": "issuer_untrusted"},
  tags=["amended-0011"])   # pinned_issuer defaults to 0x11; record's issuer is 0x33

V("I2-trusted-issuer-control", "§8.2a", "355-375",
  "the SAME record with its issuer IN the trusted set -> valid (positive control: proves I1 rejects on trust, not otherwise)",
  _untrusted_rec, {"valid": True},
  pinned_issuer=UNTRUSTED_ISSUER_PUB,
  tags=["amended-0011"])


# ── Layer-1 generator-side proofs (the invariants must hold on the BYTES) ──────
def _verify(pub_hex: str, sig_hex: str, msg: bytes) -> bool:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex)).verify(bytes.fromhex(sig_hex), msg)
        return True
    except (InvalidSignature, ValueError):
        return False


def _sysbody_bytes(rec) -> bytes:
    return _canon({k: v for k, v in rec.items() if k not in ENVELOPE_KEYS})


def _sig_hex(s) -> str:
    return s.split(":", 1)[1] if s else ""


def prove_invariants():
    """Generator-side proofs (Layer 1 re-checks these against vectors.json). These hold on
    the BYTES, before any verifier exists — that is the point of corpus-first."""
    # (a) W1 is genuinely well-formed: both signatures verify, content_digest matches.
    w1 = next(v for v in VECTORS if v["id"] == "W1-valid-bound")["subject"]
    a = w1["approval_assertion"]
    assert _verify(APPROVER_PUB, _sig_hex(w1["approver_signature"]), DOMAIN_TAG + _canon(a)), "W1 approver sig must verify"
    assert _verify(ISSUER_PUB, _sig_hex(w1["system_signature"]), _sysbody_bytes(w1)), "W1 system sig must verify"
    assert a["content_digest"] == content_digest(w1["content_body"]), "W1 content_digest must match"

    # (b) THE NESTING INVARIANT (N1), proven on the bytes, the important probe:
    #     the ORIGINAL system signature (carried unchanged on the stripped record), verified
    #     against the STRIPPED body, MUST FAIL. Not re-signed, not re-derived.
    n1 = next(v for v in VECTORS if v["id"] == "N1-stripped-approver-sig")["subject"]
    assert "approver_signature" not in n1, "N1 subject must have the approver signature stripped"
    assert not _verify(ISSUER_PUB, _sig_hex(n1["system_signature"]), _sysbody_bytes(n1)), \
        "NESTING BROKEN: original system signature must NOT verify against the stripped body"
    # control: the intact record's signature DOES verify against its intact body.
    assert _verify(ISSUER_PUB, _sig_hex(_valid_opt["system_signature"]), _sysbody_bytes(_valid_opt)), \
        "control: intact system signature must verify against the intact body"

    # (c) N2 tampered signed field likewise breaks the system signature (§8.6 body coverage).
    n2 = next(v for v in VECTORS if v["id"] == "N2-tampered-signed-field")["subject"]
    assert not _verify(ISSUER_PUB, _sig_hex(n2["system_signature"]), _sysbody_bytes(n2)), \
        "N2: tampering a signed field must break the system signature"

    # (d) K2 lift: the assertion's content_digest does NOT match the recomputed body digest.
    k2 = next(v for v in VECTORS if v["id"] == "K2-lift-to-different-content")["subject"]
    assert k2["approval_assertion"]["content_digest"] != content_digest(k2["content_body"]), \
        "K2: lifted assertion digest must not match the new content"

    # (e) THE ISSUER-PINNING PROBE (I1), mirroring the N1 invariant on the bytes (decision 0011):
    #     the untrusted record's system signature MUST verify CLEANLY under its own self-declared
    #     0x33 issuer_key_id — otherwise the vector would reject for a crypto reason, not the trust
    #     check, and prove nothing (same failure mode as recompute-over-stripped). The DEFECT is the
    #     mismatch: the record self-declares 0x33 while I1 pins 0x11.
    i1 = next(v for v in VECTORS if v["id"] == "I1-untrusted-issuer")
    i1s = i1["subject"]
    assert i1s["system_metadata"]["issuer_key_id"] == UNTRUSTED_ISSUER_KEY_ID, "I1 must self-declare 0x33"
    assert _verify(UNTRUSTED_ISSUER_PUB, _sig_hex(i1s["system_signature"]), _sysbody_bytes(i1s)), \
        "I1: system signature MUST verify under its self-declared 0x33 key (otherwise-valid; a 2a-skipping verifier passes it)"
    assert not _verify(ISSUER_PUB, _sig_hex(i1s["system_signature"]), _sysbody_bytes(i1s)), \
        "I1: the 0x33 signature must NOT verify under 0x11 (the keys are distinct)"
    assert i1["pinned_issuer"] == ISSUER_PUB and i1s["system_metadata"]["issuer_key_id"] != ISSUER_KEY_ID, \
        "I1: the DEFECT is pinned 0x11 vs self-declared 0x33 — the mismatch step 2a must catch"
    # control: I2 is the SAME record but pins 0x33 (its own issuer) — the trust check would pass.
    i2 = next(v for v in VECTORS if v["id"] == "I2-trusted-issuer-control")
    assert i2["subject"] == i1s, "I2 must be the SAME record as I1 (only the trusted set differs)"
    assert i2["pinned_issuer"] == UNTRUSTED_ISSUER_PUB, "I2 control must pin 0x33 (issuer trusted)"


def main():
    prove_invariants()
    out = {
        "corpus": "cgr.cosign.v1 conformance",
        "spec": "docs/cgr/cgr-cosign-v1-spec.md (amended by decisions 0010 and 0011)",
        "note": "TEST vectors — deterministic repeating-byte issuer key 0x11 / approver key 0x22 / "
                "UNTRUSTED issuer 0x33, NOT real keys. Profile registry (registry.json) is a TEST "
                "FIXTURE — see README. The trusted issuer set is derived per-vector from `pinned_issuer` "
                "(decision 0011 §8.2a): the trusted issuer for every vector is its pinned 0x11 key, "
                "except I2 (control) which pins 0x33.",
        "issuer_pubkey_hex": ISSUER_PUB,
        "approver_pubkey_hex": APPROVER_PUB,
        "untrusted_issuer_pubkey_hex": UNTRUSTED_ISSUER_PUB,
        "domain_tag": DOMAIN_TAG.decode(),
        "vector_count": len(VECTORS),
        "vectors": VECTORS,
    }
    (_HERE / "vectors.json").write_text(json.dumps(out, indent=2, sort_keys=False) + "\n")
    (_HERE / "issuer.json").write_text(json.dumps(
        {"issuer_pubkey_hex": ISSUER_PUB, "approver_pubkey_hex": APPROVER_PUB,
         "untrusted_issuer_pubkey_hex": UNTRUSTED_ISSUER_PUB,
         "domain_tag": DOMAIN_TAG.decode(),
         "note": "deterministic TEST keys (repeating-byte seeds 0x11 / 0x22 / 0x33 untrusted) — NOT real keys"},
        indent=2) + "\n")
    print(f"wrote {len(VECTORS)} vectors; nesting + well-formedness invariants proved on the bytes")


if __name__ == "__main__":
    main()
