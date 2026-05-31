#!/usr/bin/env python3
"""
B0 FINAL — reproduce phase1_gcrumbs_chain.json using the EXACT logic
from landing/src/grafomem_landing/crumbs.py + hashing.py.

Now we know:
  (a) breadcrumb_id = b2_128(str(seq), event_type, payload_hash, prev_id)
      where b2_128(*parts) = blake2b(US.join(p.encode() for p in parts), digest_size=16).hexdigest()
  (b) _leaf(bc) = b2_256(canon({seq, event_type, payload, prev_id}))
  (c) _merkle: hex-string concatenation Merkle — NOT R2's domain-separated tree
      Node hash = b2_256((left_hex + right_hex).encode())  — strings, not bytes!
  (d) epoch signature = sign(epoch_id bytes)  — already confirmed
  (e) epoch is cumulative prefix: all breadcrumbs [0:n_leaves]
"""
import hashlib, json, os

US = b"\x1f"

def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()

def b2_256(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=32).hexdigest()

def b2_128(*parts: str) -> str:
    h = hashlib.blake2b(digest_size=16)
    h.update(US.join(p.encode() for p in parts))
    return h.hexdigest()

def _leaf(bc: dict) -> str:
    """Leaf = b2_256(canon({seq, event_type, payload, prev_id}))"""
    return b2_256(canon({k: bc[k] for k in ["seq", "event_type", "payload", "prev_id"]}))

def _merkle(leaves: list[str]) -> str:
    """Hex-string Merkle tree. Node = b2_256((left_hex + right_hex).encode())"""
    if not leaves:
        return "0" * 64
    cur = leaves[:]
    while len(cur) > 1:
        nxt = []
        for i in range(0, len(cur), 2):
            left = cur[i]
            right = cur[i + 1] if i + 1 < len(cur) else cur[i]
            nxt.append(b2_256((left + right).encode()))
        cur = nxt
    return cur[0]

def ed_verify(pubkey_hex, message_bytes, sig_hex):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
    pub.verify(bytes.fromhex(sig_hex), message_bytes)
    return True

# ============================================================================
# Load artifact
# ============================================================================
artifact_path = os.path.join(os.path.dirname(__file__), "..",
                             "landing", "conformance", "artifacts", "phase1_gcrumbs_chain.json")
with open(artifact_path) as f:
    art = json.load(f)

bcs = sorted(art["breadcrumbs"], key=lambda b: b["seq"])
epoch1 = art["epoch1"]
epoch2 = art["epoch2"]

print("=" * 70)
print("B0: REPRODUCE PHASE1_GCRUMBS_CHAIN.JSON")
print("=" * 70)
print()

# ---- 1. CHAIN LINKAGE ----
print("1. Chain linkage (prev_id):")
prev = "0" * 32
for bc in bcs:
    assert bc["prev_id"] == prev, f"chain break at seq {bc['seq']}"
    prev = bc["breadcrumb_id"]
print(f"   ✅ 26/26 breadcrumbs chain correctly")

# ---- 2. PAYLOAD HASH ----
print("2. payload_hash = b2_256(canon(payload)):")
for bc in bcs:
    assert b2_256(canon(bc["payload"])) == bc["payload_hash"], f"payload_hash mismatch at seq {bc['seq']}"
print(f"   ✅ 26/26 payload_hash verified")

# ---- 3. BREADCRUMB_ID ----
print("3. breadcrumb_id = b2_128(str(seq), event_type, payload_hash, prev_id):")
for bc in bcs:
    computed = b2_128(str(bc["seq"]), bc["event_type"], bc["payload_hash"], bc["prev_id"])
    assert computed == bc["breadcrumb_id"], f"breadcrumb_id mismatch at seq {bc['seq']}: {computed} != {bc['breadcrumb_id']}"
print(f"   ✅ 26/26 breadcrumb_id verified")

# ---- 4. SIGNATURES ----
print("4. Ed25519 signatures on breadcrumbs:")
for bc in bcs:
    ed_verify(epoch1["sealer_pubkey"], bytes.fromhex(bc["breadcrumb_id"]), bc["signature"])
print(f"   ✅ 26/26 signatures verified")

# ---- 5. MERKLE ROOT (epoch 1) ----
print("5. Merkle root (epoch 1, 20 leaves, cumulative prefix):")
leaves1 = [_leaf(bc) for bc in bcs[:20]]
root1 = _merkle(leaves1)
assert root1 == epoch1["merkle_root"], f"root mismatch: {root1} != {epoch1['merkle_root']}"
print(f"   ✅ Merkle root matches: {root1[:24]}...")

# ---- 6. MERKLE ROOT (epoch 2) ----
print("6. Merkle root (epoch 2, 22 leaves, cumulative prefix):")
leaves2 = [_leaf(bc) for bc in bcs[:22]]
root2 = _merkle(leaves2)
assert root2 == epoch2["merkle_root"], f"root mismatch: {root2} != {epoch2['merkle_root']}"
print(f"   ✅ Merkle root matches: {root2[:24]}...")

# ---- 7. EPOCH SIGNATURES ----
print("7. Epoch signatures (sign over epoch_id bytes):")
ed_verify(epoch1["sealer_pubkey"], bytes.fromhex(epoch1["epoch_id"]), epoch1["signature"])
print(f"   ✅ epoch1 signature verified")
ed_verify(epoch2["sealer_pubkey"], bytes.fromhex(epoch2["epoch_id"]), epoch2["signature"])
print(f"   ✅ epoch2 signature verified")

# ---- 8. MEMBERSHIP ----
print("8. Epoch membership (cumulative prefix):")
print(f"   epoch1: breadcrumbs[0:20] ({epoch1['n_leaves']} leaves)")
print(f"   epoch2: breadcrumbs[0:22] ({epoch2['n_leaves']} leaves)")
print(f"   remaining: breadcrumbs[22:26] (4 not yet sealed)")
print(f"   ✅ Cumulative prefix confirmed (20 + 22 = 42 > 26)")

# ---- 9. INCLUSION PROOF (for a sample leaf) ----
print("9. Inclusion proof (sample, epoch 2, leaf 0):")
# Rebuild tree for epoch 2
all_leaves2 = [_leaf(bc) for bc in bcs[:22]]
# Get proof for leaf 0
levels = [all_leaves2[:]]
cur = all_leaves2[:]
while len(cur) > 1:
    nxt = []
    for i in range(0, len(cur), 2):
        left = cur[i]
        right = cur[i + 1] if i + 1 < len(cur) else cur[i]
        nxt.append(b2_256((left + right).encode()))
    levels.append(nxt)
    cur = nxt

# Build proof path for index 0
idx = 0
proof = []
for level in levels[:-1]:
    sib = idx ^ 1
    sib_hash = level[sib] if sib < len(level) else level[idx]
    proof.append({"hash": sib_hash, "right": (idx % 2 == 0)})
    idx //= 2

# Verify
h = all_leaves2[0]
for step in proof:
    h = b2_256((h + step["hash"]).encode()) if step["right"] else b2_256((step["hash"] + h).encode())
assert h == root2, f"inclusion proof failed: {h} != {root2}"
print(f"   ✅ Inclusion proof verified for leaf 0 ({len(proof)} steps)")

print()
print("=" * 70)
print("B0: ALL GREEN — artifact reproduced")
print("=" * 70)
print()
print("PINNED DECISIONS:")
print("  (a) breadcrumb_id = b2_128(str(seq), event_type, payload_hash_hex, prev_id_hex)")
print("      where b2_128(*parts) = blake2b(US.join(p.encode()), digest=16).hexdigest()")
print("  (b) _leaf(bc) = b2_256(canon({seq, event_type, payload, prev_id}))")
print("  (c) membership = cumulative prefix [0:n_leaves]")
print("  (d) epoch signature = Ed25519(epoch_id bytes)")
print("  (e) Merkle tree = hex-string concat: node = b2_256((left+right).encode())")
print("      NOT R2's domain-separated tree — separate implementation required")
