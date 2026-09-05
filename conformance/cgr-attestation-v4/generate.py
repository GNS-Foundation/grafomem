#!/usr/bin/env python3
"""
Generator for the cgr.attestation.v4 conformance corpus.

Produces `vectors.json` + `issuer.json`: signed test vectors that encode the
NORMATIVE MUST rules of docs/cgr/cgr-attestation-v4-spec.md. A v4 verifier is
conformant iff it returns the `expect` verdict on every vector.

Deterministic: fixed test keypairs (repeating-byte seeds — NOT real keys), so
re-running yields byte-identical output. Reuses the production canonicalization
(`aml.cgr.attestation`) so vector bytes match a real verifier's.

Resolution model (see README): a verifier is given `subject`, `ledger` (a map from
target.hash to the attestation/cert it resolves — resolution context AND queryable
index), `held_edges` (edge-records handed to the verifier, which it MUST honour), and
`pinned_issuer`. Held-edge vs ledger-only (seek) is the 0006-B pivot. Attestation
targets are addressed by BLAKE2b-256 of the canonical signed body (the deployed
`attestation_fingerprint`); delegation-cert targets by SHA-256 of the canonical cert
body (geiant `cert_hash`). §1.1.

    python3 conformance/cgr-attestation-v4/generate.py    # writes vectors.json + issuer.json
"""
from __future__ import annotations
import sys, os, json, hashlib, pathlib

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_ROOT / "src"))

import rfc8785  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: E402
from aml.cgr.attestation import canonical_body, attestation_fingerprint  # noqa: E402

# ── deterministic TEST keys (repeating-byte seeds; NOT real keys) ─────────────
ISSUER_SK = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([0x11]) * 32)
AGENT_SK = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([0x22]) * 32)
def _pub_hex(sk): return sk.public_key().public_bytes_raw().hex()
ISSUER_PUB = _pub_hex(ISSUER_SK)
AGENT_PUB = _pub_hex(AGENT_SK)

SCHEMA = "cgr.attestation.v4"


def _canon(obj) -> bytes:
    return rfc8785.dumps(obj)


def att(subject_key, *, dimension="receivables", relates_to=None, grounding=None,
        signer=ISSUER_SK, drop=(), envelope_relates_to=None, **overrides):
    """Build one signed v4 attestation. `grounding`: None=non-grounding (grounding
    fields omitted); dict=grounding-class (merged in). `drop`: field names to omit
    (to build the missing-required-field negative vectors)."""
    body = {
        "schema": SCHEMA,
        "issuer": "gns-foundation",
        "issuer_key_id": ISSUER_PUB,
        "subject_key": subject_key,
        "subject_did": "did:key:z6Mk" + subject_key[:32],
        "agent_handle": "vector-agent@test",
        "dimension": dimension,
        "cgr_score": 0.6666666666666666,
        "confidence": 6.0,
        "n_resolved": 12,
        "capability_tier": 0.75,
        "as_of": "2026-01-01T00:00:00Z",
        "last_resolved_at": "2026-01-01T00:00:00Z",
        "scoring_scope": "pooled",
        "requested_domain": None,
        "domain_n_resolved": None,
        "rationale": "conformance vector",
        # v4 additions (0002). NOTE: the default is a POOLED JUDGMENT AGGREGATE (scoring_scope=
        # "pooled", verifiability_tag="judgment"). Per the §2.2 pooled-aggregate absence gate, the
        # three PER-RECORD fields — domain, decision_date, backfilled — MUST be ABSENT here, so none
        # is in the default body; the vectors that need them (D1/P3/P5 negatives) add them via
        # **overrides. recorded_at + verifiability_tag are UNIVERSAL and stay.
        "verifiability_tag": "judgment",
        "recorded_at": "2026-01-01",
    }
    if relates_to is not None:
        body["relates_to"] = relates_to
    if grounding is not None:               # grounding-class: oracle_id + audit_policy REQUIRED
        body["oracle_id"] = grounding.get("oracle_id", "test-oracle@v1")
        body["audit_policy"] = grounding.get("audit_policy", "blake2b-256:" + "ab" * 32)
        if "n_unresolvable" in grounding:
            body["n_unresolvable"] = grounding["n_unresolvable"]
    body.update(overrides)
    for k in drop:
        body.pop(k, None)
    sig = signer.sign(_canon(body)).hex()
    out = {**body, "signature": sig, "evidence_ref": None}
    if envelope_relates_to is not None:     # B1: relates_to added AFTER signing → not covered by the
        out["relates_to"] = envelope_relates_to   # signature. Re-canonicalizing the body makes the sig fail.
    return out


def fp(a) -> str:
    """BLAKE2b-256 fingerprint of an attestation's canonical signed body (§1.1)."""
    return attestation_fingerprint(a)


def cert(agent_pk, principal_pk="bb" * 32, **overrides):
    """A mock geiant delegation cert body (format unchanged by v4)."""
    body = {
        "version": 1, "agent_pk": agent_pk, "principal_pk": principal_pk,
        "h3_cells": ["851e8053fffffff"], "facets": ["energy@italy-geiant"],
        "not_before": "2026-08-31T00:00:00.000Z", "not_after": "2027-08-31T00:00:00.000Z",
        "max_depth": 0, "constraints": {"allowed_tools": ["perception_weather"], "max_ops_per_hour": 1000},
    }
    body.update(overrides)
    return body


def cert_hash(c) -> str:
    """SHA-256 of the canonical cert body, principal_signature excluded (§1.1)."""
    return hashlib.sha256(_canon(c)).hexdigest()


def tgt_att(a): return {"kind": "attestation", "hash_alg": "blake2b-256", "hash": fp(a)}
def tgt_cert(c): return {"kind": "delegation_cert", "hash_alg": "sha-256", "hash": cert_hash(c)}


# §1.1 evidence_tier — REQUIRED on `continues`, MUST be absent on supersedes/revokes.
EVIDENCE_TIERS = ("custody_record", "issuer_records", "operator_verification")

def _edge(kind, target, tier="issuer_records"):
    """One relation edge. A `continues` edge carries the REQUIRED `evidence_tier` (§1.1);
    supersedes/revokes never do (the field is continues-only)."""
    e = {"type": kind, "target": target}
    if kind == "continues":
        e["evidence_tier"] = tier
    return e


VECTORS = []
def V(id, clause, lines, title, subject, expect, *, atts=None, certs=None, held=None,
      mode="enforcing", seek_fails=False):
    # `held` = edge-records HANDED to the verifier (MUST honour, both modes). `atts`/`certs`
    # = the `ledger`: resolution context for the subject's own edges AND the queryable index a
    # `seek` consults. `mode`: "enforcing" (verifier seeks edges targeting the subject) vs
    # "non-enforcing" (does not) — 0006-accepted enforce-or-label. `seek_fails`: harness flag —
    # make the injected seek throw (§ seek-failure → undeterminable → reject).
    entry = {
        "id": id, "clause": clause, "spec_lines": lines, "title": title,
        "mode": mode,
        "pinned_issuer": ISSUER_PUB,
        "subject": subject,
        "held_edges": list(held or []),
        "ledger": {
            "attestations": {fp(a): a for a in (atts or [])},
            "delegation_certs": {cert_hash(c): c for c in (certs or [])},
        },
        "expect": expect,
    }
    if seek_fails:
        entry["seek_fails"] = True
    VECTORS.append(entry)


SK = "aa" * 32     # a stable subject_key for most vectors
SK2 = "cc" * 32

# ── §1.1 Multiplicity ────────────────────────────────────────────────────────
_p = att(SK2)   # a predecessor to point at
V("M1-duplicate-edge", "§1.1", "98-100",
  "duplicate {type,target} pair -> reject (malformed)",
  att(SK, relates_to=[{"type": "supersedes", "target": tgt_att(_p)},
                      {"type": "supersedes", "target": tgt_att(_p)}]),
  {"valid": False, "reason_contains": "duplicate"}, atts=[_p])

_p2 = att("dd" * 32)
V("M2-two-continues", "§1.1", "101-107",
  "two continues edges (even distinct targets) -> reject (>1 lineage predecessor)",
  att(SK, relates_to=[_edge("continues", tgt_att(_p)),
                      _edge("continues", tgt_att(_p2))]),
  {"valid": False, "reason_contains": "continues"}, atts=[_p, _p2])

V("M3-two-supersedes-distinct", "§1.1", "107-109",
  "multiple supersedes to distinct targets -> valid (consolidation)",
  att(SK, relates_to=[{"type": "supersedes", "target": tgt_att(_p)},
                      {"type": "supersedes", "target": tgt_att(_p2)}]),
  {"valid": True}, atts=[_p, _p2])

V("M4-two-revokes-distinct", "§1.1", "110-111",
  "multiple revokes to distinct targets -> valid (batch revocation)",
  att(SK, relates_to=[{"type": "revokes", "target": tgt_att(_p)},
                      {"type": "revokes", "target": tgt_att(_p2)}]),
  {"valid": True}, atts=[_p, _p2])

# ── §1.1 per-kind hash_alg (normative) ───────────────────────────────────────
V("H1-attestation-wrong-alg", "§1.1", "82-98",
  "attestation target with hash_alg sha-256 -> reject (must be blake2b-256)",
  att(SK, relates_to=[{"type": "supersedes",
                       "target": {"kind": "attestation", "hash_alg": "sha-256", "hash": fp(_p)}}]),
  {"valid": False, "reason_contains": "hash_alg"}, atts=[_p])
V("H2-cert-wrong-alg", "§1.1", "82-98",
  "delegation_cert target with hash_alg blake2b-256 -> reject (must be sha-256)",
  att(SK, relates_to=[{"type": "revokes",
                       "target": {"kind": "delegation_cert", "hash_alg": "blake2b-256",
                                  "hash": cert_hash(cert(SK))}}]),
  {"valid": False, "reason_contains": "hash_alg"}, certs=[cert(SK)])

# ── §1.1 target-shape gates (enforced in both verifiers; the sweep found them vector-less) ──
V("H3-malformed-hash", "§1.1", "103",
  "target.hash not lowercase-64-hex -> reject (malformed hash)",
  att(SK, relates_to=[{"type": "revokes",
                       "target": {"kind": "attestation", "hash_alg": "blake2b-256",
                                  "hash": "NOT-HEX"}}]),
  {"valid": False, "reason_contains": "malformed target hash"})
V("H4-missing-kind", "§1.1", "85",
  "target missing kind -> reject (invalid target kind)",
  att(SK, relates_to=[{"type": "revokes",
                       "target": {"hash_alg": "blake2b-256", "hash": "aa" * 32}}]),
  {"valid": False, "reason_contains": "target kind"})
V("H5-missing-hash-alg", "§1.1", "85",
  "target missing hash_alg -> reject (hash_alg mismatch for kind)",
  att(SK, relates_to=[{"type": "revokes",
                       "target": {"kind": "attestation", "hash": "aa" * 32}}]),
  {"valid": False, "reason_contains": "hash_alg"})
V("H6-invalid-kind", "§1.1", "85",
  "target.kind not in {attestation, delegation_cert} -> reject",
  att(SK, relates_to=[{"type": "revokes",
                       "target": {"kind": "bogus", "hash_alg": "blake2b-256", "hash": "aa" * 32}}]),
  {"valid": False, "reason_contains": "target kind"})

# ── neutrality (§2.2; enforced in both verifiers, the sweep found it vector-less) ──
V("N1-self-attestation", "§2.2", "339",
  "subject_key == issuer_key_id -> reject (neutrality violation)",
  att(ISSUER_PUB),
  {"valid": False, "reason_contains": "neutrality"})

# ── §1.3 Traversal ───────────────────────────────────────────────────────────
V("T1-unknown-type", "§1.3", "178-184",
  "unrecognized relation type -> reject (fail closed)",
  att(SK, relates_to=[{"type": "corrects", "target": tgt_att(_p)}]),
  {"valid": False, "reason_contains": "unrecognized"}, atts=[_p])

# T2 / T3 / T4 / T5 cycles: the ledger supplies a deliberate pointer-cycle. A genuine
# fingerprint cycle cannot arise (§1.3), so these represent the corrupt/tampered state
# the verifier MUST detect. We index ledger entries by the hashes the edges reference.
def cycle_pair(kind_a, kind_b):
    # Legible VALID 64-hex placeholder targets (must be hex per §1.1 malformed-hash rule):
    # "cc" = cycle; 0a / 0b = the two nodes. Distinct, deterministic, byte-stable.
    hA, hB = "cc0a".ljust(64, "0"), "cc0b".ljust(64, "0")
    A = att(SK, relates_to=[_edge(kind_a, {"kind": "attestation", "hash_alg": "blake2b-256", "hash": hB})])
    B = att(SK2, relates_to=[_edge(kind_b, {"kind": "attestation", "hash_alg": "blake2b-256", "hash": hA})])
    return A, B, {hA: A, hB: B}

A, B, ledgermap = cycle_pair("continues", "continues")
V2 = {"id": "T2-continues-cycle", "clause": "§1.3", "spec_lines": "185-194",
      "title": "continues cycle -> VALID subject + lineage_status=anomaly_cycle (NOT truncated_unavailable)",
      "pinned_issuer": ISSUER_PUB, "held_edges": [], "mode": "enforcing", "subject": A,
      "ledger": {"attestations": ledgermap, "delegation_certs": {}},
      "expect": {"valid": True, "lineage_status": "anomaly_cycle"}}
VECTORS.append(V2)

A, B, ledgermap = cycle_pair("supersedes", "supersedes")
VECTORS.append({"id": "T3-supersedes-cycle", "clause": "§1.3", "spec_lines": "195-197",
                "title": "supersedes cycle -> reject (currency undeterminable)",
                "pinned_issuer": ISSUER_PUB, "held_edges": [], "mode": "enforcing", "subject": A,
                "ledger": {"attestations": ledgermap, "delegation_certs": {}},
                "expect": {"valid": False, "reason_contains": "cycle"}})

A, B, ledgermap = cycle_pair("revokes", "revokes")
VECTORS.append({"id": "T4-revokes-cycle", "clause": "§1.3", "spec_lines": "198-200",
                "title": "revokes cycle -> reject (incoherent revocation state)",
                "pinned_issuer": ISSUER_PUB, "held_edges": [], "mode": "enforcing", "subject": A,
                "ledger": {"attestations": ledgermap, "delegation_certs": {}},
                "expect": {"valid": False, "reason_contains": "cycle"}})

A, B, ledgermap = cycle_pair("continues", "supersedes")  # mixed: a supersedes edge is in the cycle
VECTORS.append({"id": "T5-mixed-cycle", "clause": "§1.3", "spec_lines": "201-203",
                "title": "mixed cycle containing a supersedes edge -> reject (conservative rule)",
                "pinned_issuer": ISSUER_PUB, "held_edges": [], "mode": "enforcing", "subject": A,
                "ledger": {"attestations": ledgermap, "delegation_certs": {}},
                "expect": {"valid": False, "reason_contains": "cycle"}})

# T6 / T7 depth: chain longer than the 64 minimum.
def deep_chain(kind, length):
    """Build a chain subject -> a1 -> a2 -> ... of `length` `kind` edges via the ledger."""
    ledger = {}
    nxt_hash = None
    for i in range(length, 0, -1):
        rel = None
        if nxt_hash is not None:
            rel = [_edge(kind, {"kind": "attestation", "hash_alg": "blake2b-256", "hash": nxt_hash})]
        node = att(f"{i:02x}" * 32, relates_to=rel)
        h = f"de{i:04d}".ljust(64, "0")   # legible valid hex: "de"=depth, 0001..0065 = chain index
        ledger[h] = node
        nxt_hash = h
    subject = att(SK, relates_to=[_edge(kind, {"kind": "attestation", "hash_alg": "blake2b-256", "hash": nxt_hash})])
    return subject, ledger

subject, ledger = deep_chain("continues", 65)
VECTORS.append({"id": "T6-continues-depth", "clause": "§1.3", "spec_lines": "204-224",
                "title": "continues chain deeper than 64 -> VALID subject + lineage_status=truncated_depth",
                "pinned_issuer": ISSUER_PUB, "held_edges": [], "mode": "enforcing", "subject": subject,
                "ledger": {"attestations": ledger, "delegation_certs": {}},
                "expect": {"valid": True, "lineage_status": "truncated_depth"}})

subject, ledger = deep_chain("supersedes", 65)
VECTORS.append({"id": "T7-supersedes-depth", "clause": "§1.3", "spec_lines": "204-224",
                "title": "supersedes chain deeper than 64 -> reject (validity undeterminable within bound)",
                "pinned_issuer": ISSUER_PUB, "held_edges": [], "mode": "enforcing", "subject": subject,
                "ledger": {"attestations": ledger, "delegation_certs": {}},
                "expect": {"valid": False, "reason_contains": "depth"}})

subject, ledger = deep_chain("revokes", 65)
VECTORS.append({"id": "T7b-revokes-depth", "clause": "§1.3", "spec_lines": "204-224",
                "title": "revokes chain deeper than 64 -> reject (validity undeterminable within bound)",
                "pinned_issuer": ISSUER_PUB, "held_edges": [], "mode": "enforcing", "subject": subject,
                "ledger": {"attestations": ledger, "delegation_certs": {}},
                "expect": {"valid": False, "reason_contains": "depth"}})

# T8 continues unreachable target: edge points at a hash NOT in the ledger.
V("T8-continues-unreachable", "§1.3", "230-239",
  "continues with unreachable predecessor -> VALID subject + lineage_status=truncated_unavailable (NOT anomaly_cycle)",
  att(SK, relates_to=[_edge("continues",
                       {"kind": "attestation", "hash_alg": "blake2b-256", "hash": "dead".ljust(64, "0")})]),
  {"valid": True, "lineage_status": "truncated_unavailable"})   # empty ledger

# T9 / T10 continues signer.
V("T9-agent-signed-continues", "§1.3/§5", "230-232",
  "continues carried by an attestation NOT signed by the pinned Foundation issuer -> reject",
  att(SK, relates_to=[_edge("continues", tgt_att(_p))], signer=AGENT_SK),
  {"valid": False, "reason_contains": "signature"}, atts=[_p])
V("T10-foundation-signed-continues", "§1.3/§5", "230-239",
  "Foundation-signed continues, predecessor resolvable -> VALID + lineage_status=complete",
  att(SK, relates_to=[_edge("continues", tgt_att(_p))]),
  {"valid": True, "lineage_status": "complete"}, atts=[_p])

# ── §1.1 evidence_tier on continues (REQUIRED; closed vocab; SURFACED, not gated) ──
_pe = att("ee" * 32)   # a resolvable predecessor for the tier vectors
V("E1-tier-custody-record", "§1.1", "104-165",
  "continues, evidence_tier=custody_record, predecessor resolvable -> VALID + complete + tier surfaced",
  att(SK, relates_to=[_edge("continues", tgt_att(_pe), "custody_record")]),
  {"valid": True, "lineage_status": "complete", "evidence_tier": "custody_record"}, atts=[_pe])
V("E2-tier-issuer-records", "§1.1", "104-165",
  "continues, evidence_tier=issuer_records -> VALID + complete + tier surfaced",
  att(SK, relates_to=[_edge("continues", tgt_att(_pe), "issuer_records")]),
  {"valid": True, "lineage_status": "complete", "evidence_tier": "issuer_records"}, atts=[_pe])
V("E3-tier-operator-verification", "§1.1", "104-165",
  "continues, evidence_tier=operator_verification (weakest) -> VALID + complete + surfaced (verifier MUST NOT gate on tier)",
  att(SK, relates_to=[_edge("continues", tgt_att(_pe), "operator_verification")]),
  {"valid": True, "lineage_status": "complete", "evidence_tier": "operator_verification"}, atts=[_pe])
V("E4-unknown-tier", "§1.1", "104-165",
  "continues with an out-of-vocabulary evidence_tier -> reject (closed vocab; fail closed)",
  att(SK, relates_to=[{"type": "continues", "target": tgt_att(_pe), "evidence_tier": "gold_standard"}]),
  {"valid": False, "reason_contains": "evidence_tier"}, atts=[_pe])
V("E5-missing-tier", "§1.1", "104-165",
  "continues with NO evidence_tier -> reject (REQUIRED on continues)",
  att(SK, relates_to=[{"type": "continues", "target": tgt_att(_pe)}]),
  {"valid": False, "reason_contains": "evidence_tier"}, atts=[_pe])
V("E6-supersedes-has-tier", "§1.1", "104-165",
  "supersedes carrying evidence_tier -> reject (evidence_tier is continues-only; misplaced field)",
  att(SK, relates_to=[{"type": "supersedes", "target": tgt_att(_pe), "evidence_tier": "issuer_records"}]),
  {"valid": False, "reason_contains": "evidence_tier"}, atts=[_pe])

# HELD-edge cases (the verifier is handed the edge -> MUST honour). Fixed verdicts;
# the SEEK counterparts (edge only in a queryable ledger) are L1/L2 (pending-0006B).
V("T13-revokes-holds-edge", "§1.3/§3", "251-253",
  "the REVOKER attestation itself (carrying a revokes edge) is well-formed -> valid",
  att(SK, relates_to=[{"type": "revokes", "target": tgt_att(_p)}]),
  {"valid": True}, atts=[_p])
V("T13b-target-of-held-revoke", "§1.3/§3", "251-253",
  "evaluating a target while the verifier HOLDS a revokes edge targeting it -> valid:false (refused)",
  _p,
  {"valid": False, "reason_contains": "revoked"},
  held=[att(SK, relates_to=[{"type": "revokes", "target": tgt_att(_p)}])])
V("T11-target-of-held-supersede", "§1.3", "240-246",
  "evaluating a target while the verifier HOLDS a supersedes edge targeting it -> valid but SUPERSEDED (stale, not current)",
  _p,
  {"valid": True, "superseded": True},
  held=[att(SK, relates_to=[{"type": "supersedes", "target": tgt_att(_p)}])])
V("T12-supersedes-unreachable", "§1.3", "247-249",
  "a superseder whose target is unreachable -> superseder stays VALID (MUST NOT reject it)",
  att(SK, relates_to=[{"type": "supersedes",
                       "target": {"kind": "attestation", "hash_alg": "blake2b-256", "hash": "dead".ljust(64, "0")}}]),
  {"valid": True})
V("T14-revokes-unreachable", "§1.3/§3", "251-253",
  "a revoker whose target is unreachable -> revoker stays VALID (revocation claim stands; target not present to refuse)",
  att(SK, relates_to=[{"type": "revokes",
                       "target": {"kind": "attestation", "hash_alg": "blake2b-256", "hash": "dead".ljust(64, "0")}}]),
  {"valid": True})

# ── cert-target vector (geiant#13; target.hash = sha-256 cert_hash) ───────────
_c = cert(SK)
V("C1-cert-target-revokes", "§1.1/§3", "89-98",
  "revokes edge targeting a delegation_cert (sha-256 cert_hash), cert resolvable -> valid revoker",
  att(SK, relates_to=[{"type": "revokes", "target": tgt_cert(_c)}]),
  {"valid": True}, certs=[_c])

# ── continues -> delegation_cert (the §5.3 ceremony output: B continues A's cert_hash) ─────────
# The rotation-continuity ceremony anchors the continues target to A's delegation cert_hash (§5.3.3).
# Two cases the served edge produces, depending on whether the verifier is handed A's cert:
_cc = cert("ee" * 32)     # A's delegation cert, RESOLVABLE (supplied in the verifier's ledger)
V("CL1-continues-cert-resolvable", "§1.3/§5.3", "778-807",
  "continues -> delegation_cert with the cert IN the ledger -> valid, lineage complete",
  att(SK, relates_to=[_edge("continues", tgt_cert(_cc), tier="custody_record")]),
  {"valid": True, "lineage_status": "complete", "evidence_tier": "custody_record"}, certs=[_cc])
_cc2 = cert("f0" * 32)    # A's cert NOT supplied — lives off-envelope (geiant delegation_certificates)
V("CL2-continues-cert-unreachable", "§1.3/§5.3", "778-807",
  "continues -> delegation_cert with the cert ABSENT from the ledger -> valid, lineage "
  "truncated_unavailable (the read surface serves B's edge but not A's cert — navigable, not resolved)",
  att(SK, relates_to=[_edge("continues", tgt_cert(_cc2), tier="operator_verification")]),
  {"valid": True, "lineage_status": "truncated_unavailable", "evidence_tier": "operator_verification"})

# ── §2.2 Grounding gate (GROUNDING_DIMENSIONS = {"grounding"}) ────────────────
V("G1-grounding-complete", "§2.2", "272-305",
  "dimension=grounding with oracle_id + audit_policy -> valid",
  att(SK, dimension="grounding", grounding={}),
  {"valid": True})
V("G2-grounding-missing-oracle", "§2.2", "304",
  "dimension=grounding, oracle_id absent -> reject",
  att(SK, dimension="grounding", grounding={}, drop=("oracle_id",)),
  {"valid": False, "reason_contains": "oracle_id"})
V("G3-grounding-missing-audit-policy", "§2.2", "304",
  "dimension=grounding, audit_policy absent -> reject",
  att(SK, dimension="grounding", grounding={}, drop=("audit_policy",)),
  {"valid": False, "reason_contains": "audit_policy"})
V("G4-nongrounding-has-oracle", "§2.2", "305",
  "dimension=receivables with oracle_id present -> reject (must be absent)",
  att(SK, dimension="receivables", oracle_id="test-oracle@v1"),
  {"valid": False, "reason_contains": "oracle_id"})
V("G5-nongrounding-has-audit-policy", "§2.2", "305",
  "dimension=receivables with audit_policy present -> reject (must be absent)",
  att(SK, dimension="receivables", audit_policy="blake2b-256:" + "ab" * 32),
  {"valid": False, "reason_contains": "audit_policy"})
V("G6-nongrounding-has-n-unresolvable", "§2.2", "305",
  "dimension=receivables with n_unresolvable present -> reject (must be absent)",
  att(SK, dimension="receivables", n_unresolvable=0),
  {"valid": False, "reason_contains": "n_unresolvable"})
V("G7-grounding-no-n-unresolvable", "§2.2", "203",
  "dimension=grounding, n_unresolvable absent -> valid (optional; defaults 0)",
  att(SK, dimension="grounding", grounding={}),
  {"valid": True})

# ── §2.2 domain gate (enforced half: absent on pooled judgment aggregates) ───
# The default att() body is a pooled judgment aggregate (scoring_scope="pooled"); the gate
# requires `domain` to be ABSENT on it. D1 carries a domain anyway -> reject. D2 is the positive
# control (pooled, no domain -> valid). The UNENFORCED half (domain REQUIRED on `rule` records)
# has NO vector by design — nothing mints a rule record yet; it gains one when something real does.
V("D1-domain-on-pooled-judgment", "§2.2", "344",
  "scoring_scope=pooled with a domain present -> reject (a pooled score has no single domain)",
  att(SK, domain="deploy"),
  {"valid": False, "reason_contains": "domain"})
V("D2-pooled-no-domain", "§2.2", "344",
  "scoring_scope=pooled with domain absent -> valid",
  att(SK),
  {"valid": True})

# ── §2.2 0002 fields — split by SCOPE (universal presence vs conditional absence-on-pooled) ──
# UNIVERSAL (every v4 record): verifiability_tag (presence + value {judgment, rule}), recorded_at
# (presence). CONDITIONAL (per-record; MUST be ABSENT on a pooled aggregate): decision_date,
# backfilled — same shape as domain (D1). Their SEMANTICS/ordering stay stated-not-enforced (§2.4).
V("P1-missing-verifiability-tag", "§2.2", "345",
  "verifiability_tag absent -> reject (universal; required on every v4 record)",
  att(SK, drop=("verifiability_tag",)),
  {"valid": False, "reason_contains": "verifiability_tag"})
V("P2-invalid-verifiability-tag", "§2.2", "345",
  "verifiability_tag not in {judgment, rule} -> reject (value is decidable)",
  att(SK, verifiability_tag="bogus"),
  {"valid": False, "reason_contains": "verifiability_tag"})
V("P4-missing-recorded-at", "§2.2", "347",
  "recorded_at absent -> reject (universal; wire-record issue time)",
  att(SK, drop=("recorded_at",)),
  {"valid": False, "reason_contains": "recorded_at"})
# P3/P5 INVERTED: decision_date / backfilled are per-record → MUST be absent on a pooled aggregate.
V("P3-decision-date-on-pooled", "§2.2", "346",
  "scoring_scope=pooled with decision_date present -> reject (a pooled score is not a single dated decision)",
  att(SK, decision_date="2026-01-01"),
  {"valid": False, "reason_contains": "decision_date"})
V("P5-backfilled-on-pooled", "§2.2", "348",
  "scoring_scope=pooled with backfilled present -> reject (meaningless without decision_date)",
  att(SK, backfilled=False),
  {"valid": False, "reason_contains": "backfilled"})

# ── §2.3 schema gate ─────────────────────────────────────────────────────────
V("S1-unknown-schema", "§2.3", "337-349",
  "schema not in ACCEPTED_SCHEMAS -> reject at the schema check",
  att(SK, schema="cgr.attestation.v5"),
  {"valid": False, "reason_contains": "schema"})
V("S2-v4-accepted", "§2.3", "337-349",
  "schema cgr.attestation.v4 -> accepted (a v4 verifier accepts v4)",
  att(SK),
  {"valid": True})

# ── §2.4 signed-body: relates_to in the envelope is unsigned -> not honoured ──
V("B1-unsigned-relates-to", "§2.4", "355-360",
  "relates_to present but NOT covered by the signature -> reject (sig fails; you cannot carry an unsigned edge)",
  att(SK, envelope_relates_to=[{"type": "revokes", "target": tgt_att(_p)}]),
  {"valid": False, "reason_contains": "signature"}, atts=[_p])

# ── §3/§4 liveness — SEEK, now decided by 0006 (enforce-or-label, accepted 2026-09-02) ─────
# The seek counterpart of the held cases (T13b/T11): the revoke/supersede edge is in the
# `ledger` (queryable) but NOT handed. An ENFORCING verifier seeks it; a NON-ENFORCING one
# does not. Both modes are vector-covered so the runner exercises each.
_revoker = att(SK2, relates_to=[{"type": "revokes", "target": tgt_att(att(SK))}])
_superseder = att(SK2, relates_to=[{"type": "supersedes", "target": tgt_att(att(SK))}])

V("L1e-revoke-seek-enforcing", "§3/§4", "423-431",
  "ENFORCING: a revokes edge targeting the subject is in the ledger (not handed) -> verifier seeks it -> valid:false (revoked)",
  att(SK), {"valid": False, "reason_contains": "revoked"}, atts=[_revoker], mode="enforcing")
V("L1n-revoke-seek-nonenforcing", "§3/§4", "423-431",
  "NON-ENFORCING: same ledger, verifier does NOT seek -> subject valid (consumer must declare + label per 0006)",
  att(SK), {"valid": True}, atts=[_revoker], mode="non-enforcing")

V("L2e-supersede-seek-enforcing", "§3/§4", "423-431",
  "ENFORCING: a supersedes edge targeting the subject is in the ledger -> verifier seeks it -> valid but superseded",
  att(SK), {"valid": True, "superseded": True}, atts=[_superseder], mode="enforcing")
V("L2n-supersede-seek-nonenforcing", "§3/§4", "423-431",
  "NON-ENFORCING: same ledger, verifier does NOT seek -> subject valid, not superseded",
  att(SK), {"valid": True}, atts=[_superseder], mode="non-enforcing")

V("L3-seek-failure-enforcing", "§3/§4", "423-431",
  "ENFORCING: seek throws/times out -> revocation status UNDETERMINABLE -> reject (NOT valid; distinct from 'revoked')",
  att(SK), {"valid": False, "reason_contains": "undeterminable"},
  atts=[_revoker], mode="enforcing", seek_fails=True)


def main():
    out = {
        "corpus": "cgr.attestation.v4 conformance",
        "note": "TEST vectors — deterministic repeating-byte issuer key, NOT a real key. "
                "See README.md for the resolution model, the seek/mode split, and the grounding-gate scope.",
        "issuer_pubkey_hex": ISSUER_PUB,
        "agent_pubkey_hex": AGENT_PUB,
        "vector_count": len(VECTORS),
        "modes": sorted({v["mode"] for v in VECTORS}),
        "vectors": VECTORS,
    }
    (_HERE / "vectors.json").write_text(json.dumps(out, indent=2, sort_keys=False) + "\n")
    (_HERE / "issuer.json").write_text(json.dumps(
        {"issuer_pubkey_hex": ISSUER_PUB, "agent_pubkey_hex": AGENT_PUB,
         "note": "deterministic TEST keys (repeating-byte seeds 0x11 / 0x22) — NOT real keys"},
        indent=2) + "\n")
    enf = sum(1 for v in VECTORS if v["mode"] == "enforcing")
    non = sum(1 for v in VECTORS if v["mode"] == "non-enforcing")
    print(f"wrote {len(VECTORS)} vectors ({enf} enforcing, {non} non-enforcing; 0 pending)")


if __name__ == "__main__":
    main()
