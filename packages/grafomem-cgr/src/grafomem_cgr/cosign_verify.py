"""cgr.cosign.v1 verifier — the second, independent reference implementation.

Written from docs/cgr/cgr-cosign-v1-spec.md §8 (amended by decision 0010), NOT ported
from clients/cgr-verify/src/cosign.js — two implementations are only worth their cost if
arrived at separately. Where this and the JS verifier disagree on a corpus vector, that
is UNDERSPECIFICATION and goes upstream as a spec finding, not reconciled here.

Target: conformance/cgr-cosign-v1/vectors.json (drive via CGR_COSIGN_VERIFIER). Reuses
the canonicalization the spec pins (RFC 8785 / JCS via `rfc8785`), Ed25519 over the raw
canonical bytes with no prehash (`cryptography`), and BLAKE2b-256 content digests
(`hashlib`). Install with the package's `verify` extra: `pip install grafomem-cgr[verify]`.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

import rfc8785
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

SCHEMA = "cgr.cosign.v1"
DOMAIN_TAG = b"grafomem.hitl.approval.v1:"                     # §9
_ENVELOPE_KEYS = ("system_signature", "evidence_ref")         # §2.3
_ORDER_OPS = ("lt", "lte", "gt", "gte")
_ALL_OPS = ("eq", "ne", "in") + _ORDER_OPS


class _Reject(Exception):
    """Fail-closed control flow: raised at the first failing §8 check, carrying the
    verdict reason. Keeps each check a small, order-explicit block (a different shape
    from the JS early-return chain — deliberately, so the two are independent)."""

    def __init__(self, reason: str):
        self.reason = reason


# ── primitives (spec-pinned) ─────────────────────────────────────────────────

def _jcs(obj: Any) -> bytes:
    return rfc8785.dumps(obj)


def _content_digest(body: Any) -> str:                          # §2.1
    return "b2-256:" + hashlib.blake2b(_jcs(body), digest_size=32).hexdigest()


def _key_bytes(key_id: Optional[str]) -> bytes:
    # accepts "ed25519:<hex>" or bare hex
    if not isinstance(key_id, str):
        raise _Reject("malformed key id")
    return bytes.fromhex(key_id.rsplit(":", 1)[-1])


def _sig_bytes(sig: Optional[str]) -> bytes:
    if not isinstance(sig, str):
        raise _Reject("malformed signature")
    return bytes.fromhex(sig.rsplit(":", 1)[-1])


def _ed25519_ok(pub_key_id: str, sig: str, msg: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(_key_bytes(pub_key_id)).verify(_sig_bytes(sig), msg)
        return True
    except (InvalidSignature, ValueError):
        return False


def _system_body(record: dict) -> bytes:                       # §2.3 — minus envelope keys
    return _jcs({k: v for k, v in record.items() if k not in _ENVELOPE_KEYS})


# ── §5.1 predicate ───────────────────────────────────────────────────────────

_SCALAR = (str, int, float, bool)


def _resolve(body: Any, path: str):
    cur = body
    for seg in str(path).split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return (False, None)
        cur = cur[seg]
    return (True, cur)


def _required_by_predicate(pred: dict, body: Any) -> bool:
    """Return True/False if the predicate resolves; raise _Reject('predicate_unresolved')
    if it cannot be meaningfully evaluated (decision 0010: UNDETERMINED -> reject).

    Independent call worth recording: an `in` whose `value` is not an array, and any op
    outside the closed set, are treated here as UNRESOLVED (a predicate that cannot be
    meaningfully evaluated is not a basis for passing) — see the report; the JS verifier
    treats a non-array `in` as simply false. No corpus vector exercises `in`, so this is
    an unpinned divergence, not a vector disagreement."""
    field, op, value = pred.get("field"), pred.get("op"), pred.get("value")
    if op not in _ALL_OPS or field is None:
        raise _Reject("predicate_unresolved: malformed predicate")
    found, v = _resolve(body, field)
    # bool is an int subclass; that is fine — it is a scalar
    if not found or v is None or not isinstance(v, _SCALAR):
        raise _Reject("predicate_unresolved: field absent, null, or non-scalar")
    if op == "eq":
        return v == value
    if op == "ne":
        return v != value
    if op == "in":
        if not isinstance(value, list):
            raise _Reject("predicate_unresolved: 'in' requires an array value")
        return v in value
    # ordering ops: numbers only (both operands), else undetermined
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not isinstance(value, (int, float)) or isinstance(value, bool):
        raise _Reject("predicate_unresolved: ordering op needs numeric operands")
    return {"lt": v < value, "lte": v <= value, "gt": v > value, "gte": v >= value}[op]


# ── §8 verifier ──────────────────────────────────────────────────────────────

def verify(record: dict, registry: dict, ledger: Optional[dict] = None) -> dict:
    """Verify a cgr.cosign.v1 record against §8, failing closed on the first failure.
    Returns {valid, reason?, surfaced?, references_unresolved?}."""
    ledger = ledger or {}
    try:
        return _verify(record, registry, ledger)
    except _Reject as r:
        return {"valid": False, "reason": r.reason}


def _verify(record: dict, registry: dict, ledger: dict) -> dict:
    if not isinstance(record, dict):
        raise _Reject("no record")
    a = record.get("approval_assertion")
    body = record.get("content_body")

    # 1 — schema
    if record.get("schema") != SCHEMA:
        raise _Reject(f"unsupported schema: {record.get('schema')}")

    # 2 — profile resolution
    entry = (registry or {}).get("profiles", {}).get(record.get("profile"))
    if entry is None:
        raise _Reject(f"unknown profile: {record.get('profile')}")
    if record.get("approval_mode") != entry.get("approval_mode"):
        raise _Reject(f"approval_mode mismatch (profile expects {entry.get('approval_mode')})")

    # 3 — content integrity. Only checkable when an assertion carries a digest; the spec
    #     assumes an assertion at §8.3 and does not state the approver-less case — recorded
    #     as an unpinned point in the report. Independent choice: skip when absent.
    if isinstance(a, dict):
        if a.get("content_digest") != _content_digest(body):
            raise _Reject("content_digest mismatch")

    # 3a — required-ness (§5.1 / decision 0010)
    req = entry.get("approver_signature")
    if req == "REQUIRED":
        required = True
    elif isinstance(req, dict) and isinstance(req.get("required_when"), dict):
        required = _required_by_predicate(req["required_when"], body)   # may raise predicate_unresolved
    else:
        raise _Reject("profile malformed: no required-ness declared")

    # 4 — approver signature
    sig = record.get("approver_signature")
    present = isinstance(sig, str) and len(sig) > 0
    if required and not present:
        raise _Reject("approver signature required but absent")
    if present:
        if not isinstance(a, dict):
            raise _Reject("approver signature present without an assertion")
        msg = DOMAIN_TAG + _jcs(a)
        if not _ed25519_ok(a.get("approver_key_id"), sig, msg):
            raise _Reject("approver signature invalid")

    # 5 — approval binds this content (explicit; the property that matters)
    if isinstance(a, dict) and a.get("content_digest") != _content_digest(body):
        raise _Reject("approval does not bind this content")

    # 6 — system signature over the body INCLUDING approver_signature (catches stripping)
    if not _ed25519_ok((record.get("system_metadata") or {}).get("issuer_key_id"),
                       record.get("system_signature"), _system_body(record)):
        raise _Reject("system signature verification failed")

    # 7 — nonce uniqueness (§4/§8.7). Data source is the deployment's; the corpus supplies
    #     ledger.seen. (Unpinned: how the verifier obtains prior nonces.)
    if isinstance(a, dict):
        pair = [a.get("approver_key_id"), a.get("record_nonce")]
        if pair in (ledger.get("seen") or []):
            raise _Reject("record_nonce replay: duplicate (approver_key_id, record_nonce)")

    # 8 — act / draft consistency (§6/§8.8)
    if isinstance(a, dict):
        act = a.get("approver_act")
        if act not in ("approve", "modify", "override"):
            raise _Reject(f"unknown approver_act: {act}")
        has_draft = "agent_draft_digest" in a
        if act == "approve" and has_draft:
            raise _Reject("agent_draft_digest must be absent for approve")
        if act in ("modify", "override") and not has_draft:
            raise _Reject(f"agent_draft_digest required for {act}")

    # 9 — free-mode references (§8.9). Field name (content_body.references) and the degrade
    #     policy are corpus/profile conventions, not spec-pinned — recorded in the report.
    references_unresolved = False
    if record.get("approval_mode") == "free":
        refs = (body or {}).get("references") if isinstance(body, dict) else None
        refs = refs if isinstance(refs, list) else []
        for r in refs:
            if not (isinstance(r, str) and r.startswith("b2-256:")
                    and len(r) == 71 and all(c in "0123456789abcdef" for c in r[7:])):
                raise _Reject(f"malformed reference hash: {r}")
        resolvable = set(ledger.get("resolvable") or [])
        references_unresolved = any(r not in resolvable for r in refs)

    out: dict = {"valid": True}
    if isinstance(a, dict):
        out["surfaced"] = {                                   # §8 surface, MUST NOT gate
            "approver_id": a.get("approver_id"),
            "approver_act": a.get("approver_act"),
            "decision_date": a.get("decision_date"),
            "assurance_tier": None,                           # the separate assurance layer (§9/§10)
        }
    if references_unresolved:
        out["references_unresolved"] = True
    return out
