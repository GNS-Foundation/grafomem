#!/usr/bin/env python3
"""Generator for the cgr.disposition.v1 RUNTIME conformance corpus.

`cgr.disposition.v1` is the accepted `cgr.cosign.v1` envelope (grafomem
`docs/cgr/cgr-cosign-v1-spec.md`, decisions 0009/0010/0011) with `profile =
"cgr.disposition.v1"`. This corpus is the executable target for the runtime record class +
the `POST /v1/dispositions` attest route + `GET /v1/dispositions/{id}` verify route.

Corpus-first (mirrors conformance/cgr-cosign-v1): fixed TEST keypairs (repeating-byte seeds,
NOT real keys); production JCS (`rfc8785`); Ed25519-over-JCS, no prehash; BLAKE2b-256 content
digest; the real DOMAIN_TAG. **Nothing AML-specific** — `content_body` is a generic opaque
object (the AML profile of the *content* lives downstream in eu-governed-agent B2, never here).

PROFILE REGISTRY — RATIFIED. The cgr.disposition.v1 profile-registry entry (approval_mode=bound, approver_signature=REQUIRED unconditional) is the normative contract at docs/cgr/cosign-profile-registry.json (decision: cosign-disposition-v1-profile-registry). The corpus registry.json mirrors it. A separate cgr.disposition.v1.predicate profile is a VERIFIER-conformance fixture only (exercises predicate_unresolved), not part of the disposition contract.

Model (spec §3.2): the **system signature is the issuer's counter-signature** — the capturing
runtime (grafomem) produces it with its pinned signing identity, and pins that key id as the sole
trusted issuer (operator decision 4). So the POST body carries the approver-signed inner
(content_body + approval_assertion + approver_signature); the runtime verifies + counter-signs +
persists. The `wrong_system_key` vector is therefore an **offline-verify** vector (a record whose
issuer_key_id is not the pinned key → verify rejects), not a POST input.

    python3 conformance/cgr-disposition-v1/generate.py   # writes vectors.json (+ issuer.json)
"""
from __future__ import annotations
import json, hashlib, pathlib, sys

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_ROOT / "src"))
import rfc8785  # production JCS
from cryptography.hazmat.primitives.asymmetric import ed25519

ISSUER_SK = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([0x11]) * 32)      # pinned runtime issuer
APPROVER_SK = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([0x22]) * 32)    # ENROLLED approver
UNENROLLED_APPROVER_SK = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([0x44]) * 32)  # NOT in the tenant registry
UNTRUSTED_ISSUER_SK = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([0x33]) * 32)     # not the pinned issuer

def _pub(sk): return sk.public_key().public_bytes_raw().hex()
SCHEMA = "cgr.cosign.v1"
PROFILE = "cgr.disposition.v1"
DOMAIN_TAG = b"grafomem.hitl.approval.v1:"
ENVELOPE_KEYS = ("system_signature", "evidence_ref")
KID = lambda sk: "ed25519:" + _pub(sk)


def _canon(o): return rfc8785.dumps(o)
def content_digest(body): return "b2-256:" + hashlib.blake2b(_canon(body), digest_size=32).hexdigest()


def assertion(body, approver_sk=APPROVER_SK, *, approver_act="approve", record_nonce="disp-0001",
              digest_override=None):
    return {
        "content_digest": digest_override or content_digest(body),
        "approver_id": "did:person:test-mlro",
        "approver_key_id": KID(approver_sk),
        "approver_act": approver_act,
        "decision_date": "2026-09-19T00:00:00Z",
        "record_nonce": record_nonce,
    }


def approver_sign(a, sk=APPROVER_SK): return "ed25519-sig:" + sk.sign(DOMAIN_TAG + _canon(a)).hex()
def system_sign(rec, sk=ISSUER_SK):
    body = {k: v for k, v in rec.items() if k not in ENVELOPE_KEYS}
    return "ed25519-sig:" + sk.sign(_canon(body)).hex()


def record(body, *, a=None, approver_sk=APPROVER_SK, include_approver=True, approver_sig=None,
           issuer_sk=ISSUER_SK, sign_system=True):
    rec = {"schema": SCHEMA, "profile": PROFILE, "approval_mode": "bound", "content_body": body}
    if include_approver:
        a = a if a is not None else assertion(body, approver_sk)
        rec["approval_assertion"] = a
        rec["approver_signature"] = approver_sig if approver_sig is not None else approver_sign(a, approver_sk)
    rec["system_metadata"] = {"issuer": "grafomem-runtime", "issuer_key_id": KID(issuer_sk),
                              "recorded_at": "2026-09-19"}
    if sign_system:
        rec["system_signature"] = system_sign(rec, issuer_sk)
    rec["evidence_ref"] = None
    return rec


# A generic (NON-AML) content body — grafomem never inspects it.
BODY = {"kind": "disposition", "subject_ref": "hmac:pseudonymous-subject-01", "decision": "approve",
        "rationale_ref": "case:opaque-01"}

VECTORS = []
def V(id, title, expect_verify, http_expect, rec, *, layer, note=""):
    VECTORS.append({"id": id, "title": title, "layer": layer, "expect_verify": expect_verify,
                    "http_expect": http_expect, "note": note, "record": rec})

# D1 — valid: approver-signed inner + runtime counter-sign. Offline verify passes; POST -> 201.
V("D1", "valid two-signature disposition", "valid", 201,
  record(BODY), layer="both",
  note="POST body = record minus system_signature/system_metadata (runtime counter-signs).")
# D2 — approver signature absent -> required (bound/REQUIRED) -> reject; POST -> 422.
V("D2", "missing approver signature", "invalid", 422,
  record(BODY, include_approver=False), layer="both",
  note="approval_assertion + approver_signature omitted; profile is REQUIRED.")
# D3 — approver key not enrolled for the tenant. Crypto self-verifies; the RUNTIME rejects
#      (approver_key_id not in hitl_approvers). Offline verify (crypto only) = valid.
V("D3", "approver key not in tenant registry", "valid", 403,
  record(BODY, approver_sk=UNENROLLED_APPROVER_SK), layer="runtime",
  note="approver_key_id 0x44 is not an enrolled approver; runtime rejects approver_not_enrolled.")
# D4 — wrong system key (issuer_key_id not the pinned runtime key). Offline-verify vector
#      (trusted={pinned 0x11}) -> issuer_untrusted. Not a POST input under the counter-sign model.
V("D4", "wrong system key (untrusted issuer)", "invalid", None,
  record(BODY, issuer_sk=UNTRUSTED_ISSUER_SK), layer="verify",
  note="issuer_key_id 0x33; verify(trusted={0x11}) rejects issuer_untrusted (decision 0011).")
# D5 — unresolvable required-ness predicate -> reject predicate_unresolved (needs a predicate
#      profile; documented in the fixture as cgr.disposition.v1.predicate).
_pred_body = {**BODY, "kind": "disposition-predicate"}
_pred_rec = record(_pred_body); _pred_rec["profile"] = "cgr.disposition.v1.predicate"
V("D5", "unresolvable required-ness predicate (verifier-only)", "invalid", None,
  _pred_rec, layer="verify",
  note="cgr.disposition.v1 required-ness is UNCONDITIONAL (ratified). This vector uses a separate verifier-fixture profile cgr.disposition.v1.predicate to exercise predicate_unresolved (0010/0011); it is NOT the disposition contract and has no runtime POST.")
# D6 — assurance marker present and SURFACED, never gated. Low assurance is accepted; the read
#      path surfaces it. content_body carries `assurance: none`.
_assure_body = {**BODY, "assurance": "none"}
V("D6", "assurance marker surfaced, never gated", "valid", 201,
  record(_assure_body), layer="both",
  note="assurance=none is accepted (201); GET/verify MUST surface it, never upgrade or gate on it.")


def main():
    out = {
        "corpus": "cgr.disposition.v1 runtime conformance",
        "spec": "docs/cgr/cgr-cosign-v1-spec.md (envelope) + decisions 0009/0010/0011",
        "profile_registry_status": "RATIFIED — docs/cgr/cosign-profile-registry.json (decision: cosign-disposition-v1-profile-registry)",
        "model": "issuer counter-signature (spec §3.2): runtime produces system_signature; pinned issuer = runtime key",
        "keys": {"pinned_issuer_pub": _pub(ISSUER_SK), "enrolled_approver_pub": _pub(APPROVER_SK),
                 "unenrolled_approver_pub": _pub(UNENROLLED_APPROVER_SK),
                 "untrusted_issuer_pub": _pub(UNTRUSTED_ISSUER_SK)},
        "domain_tag": DOMAIN_TAG.decode(),
        "vector_count": len(VECTORS),
        "vectors": VECTORS,
    }
    (_HERE / "vectors.json").write_text(json.dumps(out, indent=2) + "\n")
    (_HERE / "issuer.json").write_text(json.dumps(
        {"trusted_issuer_pub": _pub(ISSUER_SK), "note": "the SOLE trusted issuer (runtime pinned key)"}, indent=2) + "\n")
    print(f"wrote {len(VECTORS)} vectors")


if __name__ == "__main__":
    main()
