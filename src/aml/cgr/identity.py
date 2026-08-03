"""CGR identity continuity — self-certifying key-rotation chain (Ticket #7).

Reputation binds to an agent's IDENTITY ANCHOR (its genesis GEIANT pubkey), not to
whichever operational key is current. A rotation is a link `{prev_key, new_key,
seq, not_before}` signed by the OLD key — so the ordered chain from the anchor to
the current key is self-verifying with no central registrar. The Foundation never
registers identities or signs rotations; the agent controls its own key continuity.

Pure + import-isolated: stdlib only, the Ed25519 `verify` capability is INJECTED
(same discipline as attestation.py). Canonicalization of a link body reuses the
JCS canon so a link the agent signs is reproducible here byte-for-byte.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable

from aml.cgr.attestation import _canon   # RFC 8785 (JCS) — same rule the agent signs with

logger = logging.getLogger("grafomem.cgr.identity")

CGR_ROTATION_SCHEMA = "cgr.rotation.v1"

# Ed25519 multicodec prefix (0xed 0x01) for did:key; base58btc (Bitcoin alphabet).
_ED25519_MULTICODEC = b"\xed\x01"
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


@dataclass(frozen=True)
class RotationProof:
    """One self-certifying key-rotation link. `sig` is prev_key's Ed25519 signature
    over the JCS canon of {prev_key, new_key, seq, not_before}."""
    prev_key: str          # 64-hex — the key being rotated OUT (signs this link)
    new_key: str           # 64-hex — the successor key
    seq: int               # monotonic position in the chain (genesis link is seq 1)
    not_before: str        # ISO-8601 — when the successor becomes effective
    sig: str               # 128-hex Ed25519 signature by prev_key


def _link_body(p: RotationProof) -> dict:
    return {"prev_key": p.prev_key, "new_key": p.new_key, "seq": p.seq, "not_before": p.not_before}


def _b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58_ALPHABET[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))          # each leading zero byte → '1'
    return _B58_ALPHABET[0] * pad + out


def did_key(pubkey_hex: str) -> str:
    """Minimal W3C did:key for an Ed25519 public key: multibase-z(base58btc) over
    the 0xed01 multicodec prefix + the raw 32-byte key. No resolver, no did:web."""
    raw = bytes.fromhex(pubkey_hex)
    return "did:key:z" + _b58encode(_ED25519_MULTICODEC + raw)


# verify(pubkey_hex, message_bytes, sig_hex) -> bool — injected Ed25519 verify.
VerifyFn = Callable[[str, bytes, str], bool]


def verify_link(proof: RotationProof, *, verify: VerifyFn) -> bool:
    """A link is valid ONLY if `sig` verifies as prev_key's signature over the
    canonical link body. Forged/unsigned successors are rejected here — this is the
    no-stolen-reputation guard, checked before any two keys are folded into one
    identity."""
    try:
        return bool(verify(proof.prev_key, _canon(_link_body(proof)), proof.sig))
    except Exception:
        return False


def resolve_identities(proofs, *, verify: VerifyFn):
    """Fold verified rotation links into per-anchor key chains.

    Returns ``(anchor_of, current_of, frozen)``:
      - ``anchor_of[key]``   → the identity anchor for every key in a resolved chain
      - ``current_of[anchor]`` → the current (latest) operational key of that identity
      - ``frozen``           → anchors halted at a FORK (a key with ≥2 valid
                                successors) or a cycle — no silent winner is picked.

    Unverifiable links are dropped (logged, never crash). The anchor is the genesis
    key (never appears as a `new_key`); an identity has exactly one anchor.
    """
    valid = [p for p in proofs if verify_link(p, verify=verify)]
    dropped = len(list(proofs)) - len(valid)
    if dropped:
        logger.warning("CGR: dropped %d unverifiable rotation link(s) (bad/forged signature)", dropped)

    succ: dict[str, dict[str, RotationProof]] = defaultdict(dict)   # prev -> {new: proof}
    for p in valid:
        succ[p.prev_key][p.new_key] = p
    forked = {k for k, nexts in succ.items() if len(nexts) > 1}     # invariant 2: fork → freeze
    all_new = {p.new_key for p in valid}
    anchors = {p.prev_key for p in valid if p.prev_key not in all_new}

    anchor_of: dict[str, str] = {}
    current_of: dict[str, str] = {}
    frozen: set[str] = set()
    for a in anchors:
        chain = [a]
        seen = {a}
        cur = a
        while cur in succ and cur not in forked:
            nxt = next(iter(succ[cur]))
            if nxt in seen:                 # cycle guard → freeze, don't loop
                frozen.add(a)
                break
            chain.append(nxt)
            seen.add(nxt)
            cur = nxt
        if cur in forked:                   # halted at a fork point → freeze the identity there
            frozen.add(a)
        for k in chain:
            anchor_of[k] = a
        current_of[a] = chain[-1]           # keys after a fork are NOT folded in (stay separate)
    if frozen:
        logger.warning("CGR: %d identity chain(s) frozen at a fork/cycle (no successor chosen): %s",
                       len(frozen), sorted(frozen))
    return anchor_of, current_of, frozen


def key_history(anchor: str, current_of, anchor_of) -> list[str]:
    """The ordered key history for an anchor: [anchor, …, current]. Reconstructed
    from anchor_of/current_of (display / audit aid)."""
    keys = [k for k, a in anchor_of.items() if a == anchor]
    # order by walking is overkill here; anchor first, current last, others between.
    cur = current_of.get(anchor, anchor)
    ordered = [anchor] + sorted(k for k in keys if k not in (anchor, cur))
    if cur != anchor:
        ordered.append(cur)
    return ordered
