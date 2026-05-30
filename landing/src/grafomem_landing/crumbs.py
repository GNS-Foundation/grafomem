"""Reference gcrumbs: breadcrumb chain -> Merkle epoch -> O(log N) inclusion proofs.

IMPORTANT (Phase B): this is a self-contained reference so the dogfood runs in
isolation. In production DO NOT add this as a second gcrumbs implementation — reuse
the real one already shipped in src/aml/cloud/execution_receipts.py (Sprint 7b).
"""
import time
from .hashing import canon, b2_256, b2_128
from .identity import sign_hex, verify_hex, pub_hex


class Crumbs:
    def __init__(self, sealer_priv):
        self.sealer = sealer_priv
        self.sealer_pub = pub_hex(sealer_priv)
        self.breadcrumbs = []
        self.epoch = None
        self._tree = None

    @staticmethod
    def _leaf(bc: dict) -> str:
        return b2_256(canon({k: bc[k] for k in ["seq", "event_type", "payload", "prev_id"]}))

    def emit(self, event_type: str, payload: dict) -> dict:
        seq = len(self.breadcrumbs)
        prev = self.breadcrumbs[-1]["breadcrumb_id"] if self.breadcrumbs else "0" * 32
        payload_hash = b2_256(canon(payload))
        bid = b2_128(str(seq), event_type, payload_hash, prev)
        bc = {"seq": seq, "event_type": event_type, "payload": payload,
              "payload_hash": payload_hash, "prev_id": prev, "breadcrumb_id": bid}
        bc["signature"] = sign_hex(self.sealer, bytes.fromhex(bid))
        self.breadcrumbs.append(bc)
        return bc

    @staticmethod
    def _merkle(leaves):
        if not leaves:
            return ("0" * 64, [["0" * 64]])
        levels, cur = [leaves[:]], leaves[:]
        while len(cur) > 1:
            nxt = []
            for i in range(0, len(cur), 2):
                left = cur[i]
                right = cur[i + 1] if i + 1 < len(cur) else cur[i]   # duplicate last if odd
                nxt.append(b2_256((left + right).encode()))
            levels.append(nxt); cur = nxt
        return (cur[0], levels)

    def roll_epoch(self) -> dict:
        leaves = [self._leaf(bc) for bc in self.breadcrumbs]
        root, levels = self._merkle(leaves)
        self._tree = levels
        epoch_id = b2_128("epoch", root, str(time.time()))
        self.epoch = {"epoch_id": epoch_id, "merkle_root": root, "n_leaves": len(leaves),
                      "sealed_at": time.time(), "sealer_pubkey": self.sealer_pub,
                      "anchor_type": "self-sealed (RFC3161/countersignature/ledger optional)"}
        self.epoch["signature"] = sign_hex(self.sealer, bytes.fromhex(epoch_id))
        return self.epoch

    def inclusion_proof(self, idx: int):
        proof, levels = [], self._tree
        for level in levels[:-1]:
            sib = idx ^ 1
            sib_hash = level[sib] if sib < len(level) else level[idx]
            proof.append({"hash": sib_hash, "right": (idx % 2 == 0)})
            idx //= 2
        return proof

    @staticmethod
    def verify_inclusion(leaf: str, proof, root: str) -> bool:
        h = leaf
        for step in proof:
            h = b2_256((h + step["hash"]).encode()) if step["right"] else b2_256((step["hash"] + h).encode())
        return h == root

    def verify_chain(self) -> str:
        """Per-breadcrumb integrity over ALL crumbs + Merkle root over the sealed prefix."""
        prev = "0" * 32
        for bc in self.breadcrumbs:
            if b2_256(canon(bc["payload"])) != bc["payload_hash"]:
                return "tampered"
            if bc["prev_id"] != prev:
                return "tampered"
            if b2_128(str(bc["seq"]), bc["event_type"], bc["payload_hash"], bc["prev_id"]) != bc["breadcrumb_id"]:
                return "tampered"
            if not verify_hex(self.sealer_pub, bc["signature"], bytes.fromhex(bc["breadcrumb_id"])):
                return "tampered"
            prev = bc["breadcrumb_id"]
        if self.epoch:
            n = self.epoch["n_leaves"]               # an epoch seals a prefix
            root, _ = self._merkle([self._leaf(bc) for bc in self.breadcrumbs[:n]])
            if root != self.epoch["merkle_root"]:
                return "tampered"
        return "intact"
