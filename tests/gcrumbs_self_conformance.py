#!/usr/bin/env python3
"""
tests/gcrumbs_self_conformance.py — two-sided suite for the gcrumbs service.

B1-B10 conformance gates. Requires GRAFOMEM_DB_URL.
B0 (test_gcrumbs_b0.py) runs DB-free — always runs.

Run standalone:
    GRAFOMEM_DB_URL=postgresql://grafomem:grafomem_dev@localhost:5432/grafomem \
        python tests/gcrumbs_self_conformance.py

Or via the v3 runner:
    GRAFOMEM_DB_URL=... pytest tests/test_v3_conformance.py -v
"""
import json, os, sys, uuid, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

DB = os.environ.get("GRAFOMEM_DB_URL", "")

from aml.cloud.gcrumbs import (
    GcrumbsError,
    GcrumbsService,
    _leaf,
    _merkle,
    b2_128,
    b2_256,
    canon,
    verify_inclusion,
)


def main() -> int:
    if not DB:
        print("ERROR: set GRAFOMEM_DB_URL"); return 2

    key = os.urandom(32)
    svc = GcrumbsService(DB, signing_identity=_MockId(key))
    svc.ensure_schema()

    TENANT = f"gcrumbs-test-{uuid.uuid4().hex[:12]}"
    results: list[tuple[str, bool, str]] = []

    def gate(name, fn):
        try:
            ok, msg = fn()
            results.append((name, ok, msg))
        except Exception as e:
            results.append((name, False, f"exception: {e}"))

    # ---- Build chain: 10 breadcrumbs across all event families ----
    events = [
        ("landing_certificate", {"args": {"cert": "c1"}, "authorized": True, "reasons": [], "agent": "alice", "tier": "trusted"}),
        ("action:train:ok", {"args": {"model": "m1"}, "authorized": True, "reasons": [], "agent": "alice", "tier": "trusted"}),
        ("customs:seal", {"args": {"corpus": "d1"}, "authorized": True, "reasons": [], "agent": None, "tier": None}),
        ("composition", {"args": {"kind": "lora-stack"}, "authorized": True, "reasons": [], "agent": "bob", "tier": "verified"}),
        ("erasure:issued", {"args": {"fact_ref": 42}, "authorized": True, "reasons": [], "agent": "dpo", "tier": None}),
        ("landing_certificate", {"args": {"cert": "c2"}, "authorized": True, "reasons": [], "agent": "alice", "tier": "trusted"}),
        ("action:deploy:ok", {"args": {"target": "prod"}, "authorized": True, "reasons": [], "agent": "carol", "tier": "release"}),
        ("customs:seal", {"args": {"corpus": "d2"}, "authorized": True, "reasons": [], "agent": None, "tier": None}),
        ("composition", {"args": {"kind": "ensemble"}, "authorized": True, "reasons": [], "agent": "bob", "tier": "verified"}),
        ("erasure:issued", {"args": {"fact_ref": 99}, "authorized": True, "reasons": [], "agent": "dpo", "tier": None}),
    ]
    breadcrumbs = []
    for event_type, payload in events:
        bc = svc.append_breadcrumb(TENANT, event_type, payload,
                                   source_type=event_type.split(":")[0])
        breadcrumbs.append(bc)

    # Seal epoch 1 (10 breadcrumbs)
    epoch1 = svc.roll_epoch(TENANT)

    # Append 2 more, seal epoch 2 (12 breadcrumbs, cumulative)
    svc.append_breadcrumb(TENANT, "action:audit:ok",
                          {"args": {}, "authorized": True, "reasons": [], "agent": "dpo", "tier": "root"})
    svc.append_breadcrumb(TENANT, "landing_certificate",
                          {"args": {"cert": "c3"}, "authorized": True, "reasons": [], "agent": "alice", "tier": "trusted"})
    epoch2 = svc.roll_epoch(TENANT)

    # ==== GATES ====

    def b1():
        ok = (epoch1["merkle_root"] and epoch1["epoch_number"] == 1
              and epoch1["n_leaves"] == 10 and epoch1["signature"])
        return ok, "" if ok else f"epoch1 malformed: {epoch1}"
    gate("B1  append + roll → epoch sealed", b1)

    def b2():
        bcs = svc.get_breadcrumbs(TENANT, limit=epoch1["n_leaves"])
        leaves = [_leaf(bc) for bc in bcs]
        root, _ = _merkle(leaves)
        ok = root == epoch1["merkle_root"]
        return ok, "" if ok else f"root mismatch: {root[:24]}... != {epoch1['merkle_root'][:24]}..."
    gate("B2  Merkle root recomputation", b2)

    def b3():
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        ep = svc.get_epoch(TENANT, epoch1["epoch_number"])
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(ep["sealer_pubkey"]))
        try:
            pub.verify(bytes.fromhex(ep["signature"]), bytes.fromhex(ep["epoch_id"]))
            return True, ""
        except Exception as e:
            return False, f"epoch sig failed: {e}"
    gate("B3  epoch Ed25519 sig verified", b3)

    def b4():
        ok = epoch2["n_leaves"] > epoch1["n_leaves"]
        if not ok:
            return False, f"epoch2 ({epoch2['n_leaves']}) <= epoch1 ({epoch1['n_leaves']})"
        all_bcs = svc.get_breadcrumbs(TENANT, limit=1000)
        leaves1 = [_leaf(bc) for bc in all_bcs[:epoch1["n_leaves"]]]
        leaves2 = [_leaf(bc) for bc in all_bcs[:epoch2["n_leaves"]]]
        root1, _ = _merkle(leaves1)
        root2, _ = _merkle(leaves2)
        ep1 = svc.get_epoch(TENANT, 1)
        ep2 = svc.get_epoch(TENANT, 2)
        ok1 = root1 == ep1["merkle_root"]
        ok2 = root2 == ep2["merkle_root"]
        if not ok1:
            return False, f"epoch1 root mismatch after rebuild"
        if not ok2:
            return False, f"epoch2 root mismatch after rebuild"
        return True, ""
    gate("B4  cumulative prefix (2nd includes 1st)", b4)

    def b5a():
        import psycopg
        from psycopg.types.json import Jsonb
        all_bcs = svc.get_breadcrumbs(TENANT, limit=1000)
        target = all_bcs[3]
        with psycopg.connect(svc.db_url, autocommit=True) as conn:
            orig_payload = target["payload"]
            conn.execute(
                "UPDATE gcrumbs_breadcrumbs SET payload_canon = %s WHERE breadcrumb_id = %s",
                (b'{"tampered":true}', target["breadcrumb_id"]),
            )
            result = svc.verify_chain(TENANT)
            tampered = result["status"] == "tampered"
            # Restore
            conn.execute(
                "UPDATE gcrumbs_breadcrumbs SET payload_canon = %s WHERE breadcrumb_id = %s",
                (canon(orig_payload), target["breadcrumb_id"]),
            )
        if not tampered:
            return False, f"expected tampered, got {result}"
        # Verify restored
        result2 = svc.verify_chain(TENANT)
        if result2["status"] != "intact":
            return False, f"restore failed: {result2}"
        return True, ""
    gate("B5a tamper payload → chain reports tampered", b5a)

    def b5b():
        import psycopg
        ep = svc.get_epoch(TENANT, 2)
        with psycopg.connect(svc.db_url, autocommit=True) as conn:
            orig_sig = ep["signature"]
            flipped = orig_sig[:4] + ("0" if orig_sig[4] != "0" else "1") + orig_sig[5:]
            conn.execute(
                "UPDATE gcrumbs_epochs SET signature = %s WHERE epoch_id = %s",
                (flipped, ep["epoch_id"]),
            )
            result = svc.verify_chain(TENANT)
            tampered = result["status"] == "tampered"
            conn.execute(
                "UPDATE gcrumbs_epochs SET signature = %s WHERE epoch_id = %s",
                (orig_sig, ep["epoch_id"]),
            )
        if not tampered:
            return False, f"expected tampered, got {result}"
        result2 = svc.verify_chain(TENANT)
        if result2["status"] != "intact":
            return False, f"restore failed: {result2}"
        return True, ""
    gate("B5b tamper epoch sig → verification fails", b5b)

    def b6():
        for seq in [0, 5, epoch2["n_leaves"] - 1]:
            result = svc.inclusion_proof(TENANT, 2, seq)
            if not result.get("included"):
                return False, f"seq {seq} not included"
            if not result.get("proof"):
                return False, f"seq {seq} proof is empty (vacuous)"
            if not verify_inclusion(result["leaf"], result["proof"], result["merkle_root"]):
                return False, f"seq {seq} proof did not verify"
        return True, ""
    gate("B6  inclusion proof non-vacuous + verified", b6)

    def b7():
        bcs = svc.get_breadcrumbs(TENANT, limit=epoch1["n_leaves"])
        event_types = {bc["event_type"] for bc in bcs}
        required = {"landing_certificate", "customs:seal", "composition", "erasure:issued"}
        action_ok = any(et.startswith("action:") for et in event_types)
        missing = required - event_types
        if missing:
            return False, f"missing event types: {missing}"
        if not action_ok:
            return False, f"no action:* events found in {event_types}"
        return True, ""
    gate("B7  cross-service breadcrumbs in one epoch", b7)

    def b8():
        empty_tenant = f"gcrumbs-empty-{uuid.uuid4().hex[:12]}"
        try:
            svc.roll_epoch(empty_tenant)
            return False, "expected GcrumbsError, got success"
        except GcrumbsError:
            return True, ""
    gate("B8  empty epoch refused", b8)

    def b9():
        import psycopg
        all_bcs = svc.get_breadcrumbs(TENANT, limit=1000)
        if len(all_bcs) < 7:
            return False, f"need at least 7 breadcrumbs, got {len(all_bcs)}"
        target = all_bcs[5]
        next_bc = all_bcs[6]
        with psycopg.connect(svc.db_url, autocommit=True) as conn:
            orig_prev = next_bc["prev_id"]
            flipped = orig_prev[:4] + ("0" if orig_prev[4] != "0" else "1") + orig_prev[5:]
            conn.execute(
                "UPDATE gcrumbs_breadcrumbs SET prev_id = %s WHERE breadcrumb_id = %s",
                (flipped, next_bc["breadcrumb_id"]),
            )
            result = svc.verify_chain(TENANT)
            tampered = result["status"] == "tampered"
            conn.execute(
                "UPDATE gcrumbs_breadcrumbs SET prev_id = %s WHERE breadcrumb_id = %s",
                (orig_prev, next_bc["breadcrumb_id"]),
            )
        if not tampered:
            return False, f"expected tampered, got {result}"
        result2 = svc.verify_chain(TENANT)
        if result2["status"] != "intact":
            return False, f"restore failed: {result2}"
        return True, ""
    gate("B9  chain verification detects tampered prev_id", b9)

    def b10():
        bcs = svc.get_breadcrumbs(TENANT, limit=1)
        ok = bcs[0]["prev_id"] == "0" * 32
        return ok, "" if ok else f"genesis prev_id: {bcs[0]['prev_id']}"
    gate("B10 genesis prev_id = '0' * 32", b10)

    # ---- Cleanup ----
    try:
        import psycopg
        with psycopg.connect(svc.db_url, autocommit=True) as conn:
            conn.execute("DELETE FROM gcrumbs_breadcrumbs WHERE tenant_id = %s", (TENANT,))
            conn.execute("DELETE FROM gcrumbs_epochs WHERE tenant_id = %s", (TENANT,))
    except Exception:
        pass

    # ---- Report ----
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n  gcrumbs self-conformance  (tenant {TENANT})\n  " + "-" * 52)
    for name, ok, msg in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"\n        -> {msg}" if not ok else ""))
    print("  " + "-" * 52)
    print(f"  {passed}/{len(results)} gates green\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())


class _MockId:
    def __init__(self, k): self.k = k
    def sign(self, m): 
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        priv = Ed25519PrivateKey.from_private_bytes(self.k)
        return priv.sign(m), priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    def public_key(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return Ed25519PrivateKey.from_private_bytes(self.k).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
