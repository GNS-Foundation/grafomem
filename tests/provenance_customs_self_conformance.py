#!/usr/bin/env python3
"""
tests/provenance_customs_self_conformance.py — two-sided suite for Data-Provenance Customs (R2).

In-process against the live service + real Postgres (GRAFOMEM_DB_URL). Non-vacuous: seals
cleared corpora and REFUSES uncleared ones (no lawful basis / no bias examination), proves
Merkle inclusion for members and rejects non-members and tampered leaves, and confirms the
sealed merkle_root/corpus_hash are the block R3 landing consumes.

    GRAFOMEM_DB_URL=postgresql://grafomem:grafomem_dev@localhost:5432/grafomem \
        python3 tests/provenance_customs_self_conformance.py
"""
from __future__ import annotations
import os, sys, uuid

from aml.cloud.provenance_customs import (
    ProvenanceCustomsService, CorpusRegisterRequest, CustomsError, CustomsRejected,
    compute_corpus_id, merkle_root, merkle_proof, verify_merkle_proof, _leaf, b2_256,
)
from psycopg.types.json import Jsonb

DB = os.environ.get("GRAFOMEM_DB_URL")
TENANT = f"r2conf-{uuid.uuid4().hex[:8]}"


class _Log:
    def __init__(self, result): self.result = result

class _Gateway:
    def __init__(self, allowed, result=None): self._a, self._r = allowed, result
    def evaluate_and_gate(self, tenant, operation, context):
        return (self._a, [] if self._a else [_Log(self._r)])


def good_corpus(name="acme-finance-corpus") -> CorpusRegisterRequest:
    return CorpusRegisterRequest(
        name=name,
        sources=[
            {"id": "src-eurlex", "license": "CC-BY-4.0", "content_hash": b2_256(b"eurlex"), "record_count": 12000},
            {"id": "src-filings", "lawful_basis": "legitimate_interest", "content_hash": b2_256(b"filings"), "record_count": 8000},
            {"id": "src-news", "license": "licensed-commercial", "content_hash": b2_256(b"news"), "record_count": 4000},
        ],
        attestations={"representativeness": "EU-27 coverage, 2019-2024",
                      "bias_examination": "demographic + sector balance reviewed",
                      "data_gaps": "low coverage of micro-enterprises noted"},
        processing=["dedup", "pii-redaction"],
        metadata={"owner": "camilo@acme"},
    )


def main() -> int:
    if not DB:
        print("ERROR: set GRAFOMEM_DB_URL"); return 2

    key = os.urandom(32)
    svc = ProvenanceCustomsService(DB, signing_key=key)
    svc.ensure_schema()

    results, st = [], {}

    def gate(name, fn):
        try:
            fn(); results.append((name, True, ""))
        except AssertionError as e:
            results.append((name, False, str(e) or "assertion failed"))
        except Exception as e:
            results.append((name, False, f"{type(e).__name__}: {e}"))

    def p1():
        c = svc.register_corpus(TENANT, good_corpus())
        assert c.get("corpus_id") and c.get("merkle_root") and c.get("corpus_hash"), "missing seal fields"
        st["cid"] = c["corpus_id"]
    gate("P1  seal cleared corpus", p1)

    def p2():
        row = svc.get_corpus(TENANT, st["cid"])
        assert row.get("document"), "no signed receipt stored"
        assert len(row["document"]["sources"]) == 3, "sources lost"
    gate("P2  persistence round-trip", p2)

    def p3():
        v = svc.verify_corpus(TENANT, st["cid"])
        assert v["passed"], f"authentic receipt failed verify: {v['checks']}"
        assert v["checks"]["signature"] and v["checks"]["merkle_consistent"] and v["checks"]["cleared"], v["checks"]
    gate("P3  customs receipt signed + verifies", p3)

    def p4():
        # dedicated corpus so this gate is order-independent and doesn't disturb st['cid']
        cid = svc.register_corpus(TENANT, good_corpus("tamper-test"))["corpus_id"]
        # (a) tamper a sealed SOURCE -> caught by the merkle/sources binding
        #     (NOT the signature: the signature binds the commitments, which verify re-derives)
        doc = svc.get_corpus(TENANT, cid)["document"]
        doc["sources"][0]["license"] = "TAMPERED"
        with svc._get_conn() as c, c.cursor() as cur:
            cur.execute("UPDATE provenance_corpora SET document=%s WHERE corpus_id=%s", (Jsonb(doc), cid))
        v = svc.verify_corpus(TENANT, cid)
        assert not v["checks"]["merkle_consistent"], "source tamper not caught by merkle root"
        assert not v["checks"]["sources_consistent"], "source tamper not caught by sources digest"
        assert not v["passed"], "tampered corpus still passed"
        # (b) tamper a SIGNED field (a commitment) -> caught by the signature
        doc["corpus_hash"] = "ff" * 32
        with svc._get_conn() as c, c.cursor() as cur:
            cur.execute("UPDATE provenance_corpora SET document=%s WHERE corpus_id=%s", (Jsonb(doc), cid))
        v2 = svc.verify_corpus(TENANT, cid)
        assert not v2["checks"]["signature"], "signed-field tamper not caught by signature"
    gate("P4  tamper-evidence: signature + merkle (non-vacuous)", p4)

    def p5():
        # deterministic: re-sealing identical content yields the same merkle root + corpus_id
        c2 = svc.register_corpus(TENANT, good_corpus())
        leaves = [_leaf(s) for s in good_corpus().sources]
        assert c2["merkle_root"] == merkle_root(leaves).hex(), "merkle root not deterministic"
        assert c2["corpus_id"] == compute_corpus_id(TENANT, "acme-finance-corpus", c2["merkle_root"]), "corpus_id mismatch"
    gate("P5  deterministic Merkle root + corpus id", p5)

    def p6():
        # NON-VACUOUS Merkle inclusion: member proves in, non-member is out, tampered leaf fails
        pr = svc.inclusion_proof(TENANT, st["cid"], "src-filings")
        assert pr["included"] is True, "member source not proven in corpus"
        out = svc.inclusion_proof(TENANT, st["cid"], "src-not-here")
        assert out["included"] is False, "non-member source proven in corpus"
        # tamper the leaf -> proof must fail against the root
        root = bytes.fromhex(pr["merkle_root"])
        assert verify_merkle_proof(bytes.fromhex(pr["leaf"]), pr["proof"], root) is True
        assert verify_merkle_proof(b"\x00" * 32, pr["proof"], root) is False, "forged leaf accepted"
    gate("P6  Merkle inclusion proof (non-vacuous)", p6)

    def p7():
        bad = good_corpus("no-basis")
        bad.sources[1].pop("lawful_basis", None)                 # remove the only basis on src-filings
        try:
            svc.register_corpus(TENANT, bad); assert False, "corpus with unlicensed source sealed"
        except CustomsRejected as e:
            assert any("lawful_basis" in r or "license" in r for r in e.reasons), e.reasons
    gate("P7  Article-10: refuse source without lawful basis", p7)

    def p8():
        bad = good_corpus("no-bias")
        bad.attestations.pop("bias_examination", None)
        try:
            svc.register_corpus(TENANT, bad); assert False, "corpus without bias examination sealed"
        except CustomsRejected as e:
            assert any("bias_examination" in r for r in e.reasons), e.reasons
    gate("P8  Article-10: refuse missing bias examination", p8)

    def p9():
        denier = ProvenanceCustomsService(DB, signing_key=key, gateway=_Gateway(False, "denied"))
        try:
            denier.register_corpus(TENANT, good_corpus("gov-denied")); assert False, "governance deny not enforced"
        except CustomsRejected:
            pass
    gate("P9  governance deny enforced", p9)

    def p10():
        # R2 -> R3 feed: the block landing consumes
        blk = svc.provenance_block(TENANT, st["cid"])
        assert blk["merkle_root"] and len(blk["merkle_root"]) == 64, "merkle_root not landing-ready"
        assert blk["corpus_hash"] and len(blk["corpus_hash"]) == 64, "corpus_hash not landing-ready"
        assert "src-filings" in blk["sources"], "sources not exposed"
    gate("P10 R2->R3 provenance block (landing-ready)", p10)

    try:
        with svc._get_conn() as c, c.cursor() as cur:
            cur.execute("DELETE FROM provenance_corpora WHERE tenant_id=%s", (TENANT,))
    except Exception:
        pass

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n  provenance-customs self-conformance  (tenant {TENANT})\n  " + "-" * 56)
    for name, ok, msg in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"\n        -> {msg}" if not ok else ""))
    print("  " + "-" * 56)
    print(f"  {passed}/{len(results)} gates green\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
