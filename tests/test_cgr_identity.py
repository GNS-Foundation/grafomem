"""CGR Ticket #7 — identity continuity across key rotation (grafomem 7a).

Covers the pure rotation primitive (did:key, verify_link, resolve_identities incl.
the no-stolen-reputation + fork-freeze invariants) and the engine's anchor-aware
aggregation (a rotated agent's whole key-history rolls up to one identity;
subject_key = current op key, subject_did = anchor did:key).
"""
from __future__ import annotations

import json
import pathlib

from aml.cgr.attestation import _canon
from aml.cgr.engine import compute_scores_from_rows, to_tiergate
from aml.cgr.identity import RotationProof, _link_body, did_key, resolve_identities, verify_link
from aml.cgr.issuance import FoundationIdentity, make_verifier
from aml.cgr.substrate import DecisionRow

_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


def _id(seed_byte: str) -> FoundationIdentity:
    return FoundationIdentity(bytes.fromhex(seed_byte * 32))


def _pub(seed_byte: str) -> str:
    return _id(seed_byte).public_key().hex()


def _verify(pubkey_hex: str, msg: bytes, sig_hex: str) -> bool:
    return make_verifier(bytes.fromhex(pubkey_hex))(msg, sig_hex)


def _link(prev_seed: str, new_seed: str, *, seq: int = 1,
          not_before: str = "2026-01-01T00:00:00Z", sign_seed: str | None = None) -> RotationProof:
    """A rotation link normally signed by prev_key; `sign_seed` overrides the signer
    to forge one (signed by a stranger)."""
    prev_key, new_key = _pub(prev_seed), _pub(new_seed)
    body = {"prev_key": prev_key, "new_key": new_key, "seq": seq, "not_before": not_before}
    sig, _p = _id(sign_seed or prev_seed).sign(_canon(body))
    return RotationProof(prev_key=prev_key, new_key=new_key, seq=seq, not_before=not_before, sig=sig.hex())


def _row(inv: str, key: str, handle: str = "finance@zurich", outcome: str = "paid") -> DecisionRow:
    return DecisionRow(decision_id=f"dec-{inv}", invoice_ref=inv, agent_handle=handle,
                       agent_tier=None, decision="certify", reason_code="clean",
                       verifiability_tag="judgment", created_at=None, outcome=outcome,
                       outcome_date=None, agent_key=key)


# --- did:key ----------------------------------------------------------------

def test_did_key_format_and_deterministic():
    d = did_key(_pub("33"))
    assert d.startswith("did:key:z6Mk")      # multibase-z + Ed25519 multicodec (0xed01)
    assert did_key(_pub("33")) == d           # deterministic
    assert did_key(_pub("44")) != d           # distinct keys → distinct DIDs


# --- verify_link (the no-stolen-reputation primitive) -----------------------

def test_verify_link_valid_forged_and_tampered():
    good = _link("aa", "bb")
    assert verify_link(good, verify=_verify) is True
    forged = _link("aa", "bb", sign_seed="ee")            # signed by a stranger, not prev_key
    assert verify_link(forged, verify=_verify) is False
    tampered = RotationProof(good.prev_key, good.new_key, good.seq, "2099-01-01T00:00:00Z", good.sig)
    assert verify_link(tampered, verify=_verify) is False  # body changed ⇒ signature no longer valid


# --- resolve_identities -----------------------------------------------------

def test_resolve_chain_folds_to_anchor():
    A, B, C = _pub("aa"), _pub("bb"), _pub("cc")
    anchor_of, current_of, frozen = resolve_identities(
        [_link("aa", "bb", seq=1), _link("bb", "cc", seq=2)], verify=_verify)
    assert anchor_of[A] == anchor_of[B] == anchor_of[C] == A   # all fold to the genesis anchor
    assert current_of[A] == C                                   # latest operational key
    assert not frozen


def test_resolve_fork_freezes_no_silent_winner():
    A, B, C = _pub("aa"), _pub("bb"), _pub("cc")
    anchor_of, current_of, frozen = resolve_identities(
        [_link("aa", "bb"), _link("aa", "cc")], verify=_verify)   # A → B and A → C
    assert A in frozen
    assert current_of[A] == A                                   # frozen at the fork, no pick
    assert B not in anchor_of and C not in anchor_of            # post-fork keys not folded in


# --- engine aggregation by anchor -------------------------------------------

def test_continuity_one_identity_across_rotation():
    A, B = _pub("aa"), _pub("bb")
    rows = [_row("X", A, outcome="paid"), _row("Y", B, outcome="default")]  # decisions under A then B
    res = compute_scores_from_rows(rows, rotations=[_link("aa", "bb")], verify=_verify)
    assert len(res) == 1                                        # ONE identity across the rotation
    tg = to_tiergate(res[0])
    assert tg["subject_key"] == B                               # current operational key
    assert tg["subject_did"] == did_key(A)                     # stable anchor did:key


def test_no_stolen_reputation_forged_rotation():
    A, B = _pub("aa"), _pub("bb")
    rows = [_row("X", A, outcome="paid"), _row("Y", B, outcome="default")]
    res = compute_scores_from_rows(rows, rotations=[_link("aa", "bb", sign_seed="ee")], verify=_verify)
    assert len(res) == 2                                        # B did NOT inherit A → two identities
    assert {r.subject_key for r in res} == {A, B}


def test_no_rotation_backward_consistent_with_5a():
    A = _pub("aa")
    rows = [_row("X", A, outcome="paid"), _row("Y", A, outcome="default")]
    res = compute_scores_from_rows(rows)                        # no rotations/verify (the #5a path)
    assert len(res) == 1
    tg = to_tiergate(res[0])
    assert tg["subject_key"] == A
    assert tg["subject_did"] == did_key(A)                     # anchor == current when never rotated


# --- #10a chain-emit golden fixture (the 10b cross-repo contract) -----------

def test_chain_golden_fixture_self_certifying_and_byte_parity():
    fx = json.loads((_FIXTURES / "cgr_rotation_chain_jcs.golden.json").read_text())
    proofs = [RotationProof(**p) for p in fx["proofs"]]

    for p, canon_expected in zip(proofs, fx["canonical_link_bodies_utf8"]):
        assert verify_link(p, verify=_verify) is True           # self-certifying: prev_key signed it
        # byte-parity: JCS canon of {prev,new,seq,not_before} == the bytes prev_key signed
        assert _canon(_link_body(p)).decode("utf-8") == canon_expected

    anchor_of, current_of, frozen = resolve_identities(proofs, verify=_verify)
    assert anchor_of[fx["current_key"]] == fx["anchor_key"]     # chain terminates at current
    assert current_of[fx["anchor_key"]] == fx["current_key"]
    assert not frozen
    assert did_key(fx["anchor_key"]) == fx["subject_did"]        # did:key(anchor) == the anchor DID

    # cross-repo contract: this chain matches the #7 v2 attestation fixture exactly
    v2 = json.loads((_FIXTURES / "cgr_attestation_v2_jcs.golden.json").read_text())
    assert fx["current_key"] == v2["subject_key"]                # chain terminates at att.subject_key
    assert fx["subject_did"] == v2["subject_did"]                # did(anchor) == att.subject_did
