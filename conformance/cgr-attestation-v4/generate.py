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
        # v4 additions (0002):
        "domain": "deploy",
        "verifiability_tag": "judgment",
        "decision_date": "2026-01-01",
        "recorded_at": "2026-01-01",
        "backfilled": False,
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
  att(SK, relates_to=[{"type": "continues", "target": tgt_att(_p)},
                      {"type": "continues", "target": tgt_att(_p2)}]),
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
    A = att(SK, relates_to=[{"type": kind_a, "target": {"kind": "attestation", "hash_alg": "blake2b-256", "hash": hB}}])
    B = att(SK2, relates_to=[{"type": kind_b, "target": {"kind": "attestation", "hash_alg": "blake2b-256", "hash": hA}}])
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
            rel = [{"type": kind, "target": {"kind": "attestation", "hash_alg": "blake2b-256", "hash": nxt_hash}}]
        node = att(f"{i:02x}" * 32, relates_to=rel)
        h = f"de{i:04d}".ljust(64, "0")   # legible valid hex: "de"=depth, 0001..0065 = chain index
        ledger[h] = node
        nxt_hash = h
    subject = att(SK, relates_to=[{"type": kind, "target": {"kind": "attestation", "hash_alg": "blake2b-256", "hash": nxt_hash}}])
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
  att(SK, relates_to=[{"type": "continues",
                       "target": {"kind": "attestation", "hash_alg": "blake2b-256", "hash": "dead".ljust(64, "0")}}]),
  {"valid": True, "lineage_status": "truncated_unavailable"})   # empty ledger

# T9 / T10 continues signer.
V("T9-agent-signed-continues", "§1.3/§5", "230-232",
  "continues carried by an attestation NOT signed by the pinned Foundation issuer -> reject",
  att(SK, relates_to=[{"type": "continues", "target": tgt_att(_p)}], signer=AGENT_SK),
  {"valid": False, "reason_contains": "signature"}, atts=[_p])
V("T10-foundation-signed-continues", "§1.3/§5", "230-239",
  "Foundation-signed continues, predecessor resolvable -> VALID + lineage_status=complete",
  att(SK, relates_to=[{"type": "continues", "target": tgt_att(_p)}]),
  {"valid": True, "lineage_status": "complete"}, atts=[_p])

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
