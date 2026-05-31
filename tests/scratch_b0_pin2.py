#!/usr/bin/env python3
"""
B0 round 2 — deeper hypothesis testing for Merkle root and epoch signature.

We know:
  (a) breadcrumb_id = b2_128(US.join(hex strings)) ✅
  Chain and signatures verified ✅

Now testing:
  (b) leaf preimage — more variants
  (d) epoch signed view — more variants
  (c) membership — confirmed cumulative via 20+22>26
"""
import hashlib, json, os

US = b"\x1f"

def b2_128(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=16).digest()

def b2_256(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()

def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()

# R2 Merkle
def _m_leaf(data: bytes) -> bytes:
    return hashlib.blake2b(b"\x00" + data, digest_size=32).digest()

def _m_node(a: bytes, b: bytes) -> bytes:
    return hashlib.blake2b(b"\x01" + a + b, digest_size=32).digest()

def merkle_root_r2(leaves: list[bytes]) -> bytes:
    """R2's tree with domain separation."""
    if not leaves:
        return hashlib.blake2b(b"\x02empty", digest_size=32).digest()
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_m_node(level[i], level[i+1]) for i in range(0, len(level), 2)]
    return level[0]

def merkle_root_plain(leaves: list[bytes]) -> bytes:
    """Plain tree: no domain separation, just b2_256(a + b)."""
    if not leaves:
        return b2_256(b"empty")
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [b2_256(level[i] + level[i+1]) for i in range(0, len(level), 2)]
    return level[0]

def merkle_root_sha256(leaves: list[bytes]) -> bytes:
    """SHA-256 tree (Bitcoin-style)."""
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hashlib.sha256(level[i] + level[i+1]).digest() for i in range(0, len(level), 2)]
    return level[0]

def ed_verify(pubkey_hex, message, sig_hex):
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        pub.verify(bytes.fromhex(sig_hex), message)
        return True
    except:
        return False

# Load artifact
artifact_path = os.path.join(os.path.dirname(__file__), "..",
                             "landing", "conformance", "artifacts", "phase1_gcrumbs_chain.json")
with open(artifact_path) as f:
    art = json.load(f)

bcs = sorted(art["breadcrumbs"], key=lambda b: b["seq"])
epochs = [art[k] for k in sorted(art.keys()) if k.startswith("epoch")]

# ============================================================================
# Leaf preimage — exhaustive search
# ============================================================================
print("=" * 70)
print("LEAF PREIMAGE HYPOTHESES (epoch 1, 20 leaves)")
print("=" * 70)
expected = epochs[0]["merkle_root"]

leaf_generators = {
    # Bytes inputs
    "breadcrumb_id bytes": lambda bc: bytes.fromhex(bc["breadcrumb_id"]),
    "payload_hash bytes": lambda bc: bytes.fromhex(bc["payload_hash"]),
    "sig bytes": lambda bc: bytes.fromhex(bc["signature"]),

    # Hex string inputs (treated as raw bytes via encode)
    "breadcrumb_id hex utf8": lambda bc: bc["breadcrumb_id"].encode(),
    "payload_hash hex utf8": lambda bc: bc["payload_hash"].encode(),

    # Hash of breadcrumb_id
    "b2_256(breadcrumb_id bytes)": lambda bc: b2_256(bytes.fromhex(bc["breadcrumb_id"])),
    "b2_256(payload_hash bytes)": lambda bc: b2_256(bytes.fromhex(bc["payload_hash"])),

    # Canon variants
    "canon(full bc)": lambda bc: canon(bc),
    "canon(bc no sig)": lambda bc: canon({k: bc[k] for k in bc if k != "signature"}),
    "canon(payload)": lambda bc: canon(bc["payload"]),
    "canon(bc minimal)": lambda bc: canon({
        "breadcrumb_id": bc["breadcrumb_id"],
        "payload_hash": bc["payload_hash"],
        "prev_id": bc["prev_id"],
    }),

    # US-join variants (matching the confirmed breadcrumb_id pattern)
    "US.join(seq,type,ph,prev,bid)": lambda bc: US.join([
        str(bc["seq"]).encode(), bc["event_type"].encode(),
        bc["payload_hash"].encode(), bc["prev_id"].encode(),
        bc["breadcrumb_id"].encode(),
    ]),
    "US.join(bid,ph)": lambda bc: US.join([
        bc["breadcrumb_id"].encode(), bc["payload_hash"].encode(),
    ]),
}

tree_fns = {
    "R2 (domain-sep)": merkle_root_r2,
    "plain (b2_256)": merkle_root_plain,
}

for leaf_name, leaf_fn in leaf_generators.items():
    for tree_name, tree_fn in tree_fns.items():
        raw_leaves = [leaf_fn(bc) for bc in bcs[:20]]

        # Try: raw leaves directly into tree
        try:
            root1 = tree_fn(raw_leaves)
        except:
            root1 = None

        # Try: _m_leaf(raw) then into R2 tree (double hashing)
        hashed_leaves = [_m_leaf(l) for l in raw_leaves]
        root2 = tree_fn(hashed_leaves)

        for root, variant in [(root1, "direct"), (root2, "+_m_leaf")]:
            if root and root.hex() == expected:
                print(f"  ✅ MATCH: leaf={leaf_name}, tree={tree_name}, variant={variant}")

# Try: no hashing at all, just treat leaves as-is if 32 bytes
print("\n  Trying raw 32-byte leaves as tree nodes (no leaf hash):")
for leaf_name, leaf_fn in leaf_generators.items():
    raw = [leaf_fn(bc) for bc in bcs[:20]]
    if all(len(l) == 32 for l in raw):
        # Plain: just b2_256(a+b) pairing
        root = merkle_root_plain(raw)
        if root.hex() == expected:
            print(f"    ✅ MATCH: {leaf_name} plain")
        # R2 domain-sep on nodes only
        root2 = merkle_root_r2(raw)
        if root2.hex() == expected:
            print(f"    ✅ MATCH: {leaf_name} R2-nodes")

# ============================================================================
# Maybe the tree is NOT using R2's construction at all
# Try: simple recursive b2_256 without ANY prefixes
# ============================================================================
print("\n  Trying completely prefix-free Merkle:")
def merkle_root_nopfx(leaves):
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hashlib.blake2b(level[i] + level[i+1], digest_size=32).digest()
                 for i in range(0, len(level), 2)]
    return level[0]

for leaf_name, leaf_fn in leaf_generators.items():
    raw = [leaf_fn(bc) for bc in bcs[:20]]
    root = merkle_root_nopfx(raw)
    if root.hex() == expected:
        print(f"    ✅ MATCH: {leaf_name} nopfx")

    # Also try hashing each raw leaf first with b2_256
    hashed = [b2_256(l) for l in raw]
    root2 = merkle_root_nopfx(hashed)
    if root2.hex() == expected:
        print(f"    ✅ MATCH: b2_256({leaf_name}) nopfx")

# ============================================================================
# Epoch signature — more hypotheses
# ============================================================================
print("\n" + "=" * 70)
print("EPOCH SIGNATURE HYPOTHESES")
print("=" * 70)
ep = epochs[0]

signed_views = {
    "canon(6 fields sorted)": canon({k: ep[k] for k in
        ("epoch_id", "merkle_root", "n_leaves", "sealed_at", "sealer_pubkey", "anchor_type")}),

    "canon(all non-sig)": canon({k: v for k, v in ep.items() if k != "signature"}),

    "canon(artifact order)": json.dumps({
        "epoch_id": ep["epoch_id"],
        "merkle_root": ep["merkle_root"],
        "n_leaves": ep["n_leaves"],
        "sealed_at": ep["sealed_at"],
        "sealer_pubkey": ep["sealer_pubkey"],
        "anchor_type": ep["anchor_type"],
    }, separators=(",", ":")).encode(),

    "b2_256(canon(6 fields))": b2_256(canon({k: ep[k] for k in
        ("epoch_id", "merkle_root", "n_leaves", "sealed_at", "sealer_pubkey", "anchor_type")})),

    "b2_256(canon(all non-sig))": b2_256(canon({k: v for k, v in ep.items() if k != "signature"})),

    "epoch_id bytes": bytes.fromhex(ep["epoch_id"]),

    "merkle_root bytes": bytes.fromhex(ep["merkle_root"]),

    "b2_256(epoch_id+root)": b2_256(bytes.fromhex(ep["epoch_id"]) + bytes.fromhex(ep["merkle_root"])),

    "US.join(epoch_id,root,n,sealed,pub,anchor) hex": US.join([
        ep["epoch_id"].encode(), ep["merkle_root"].encode(),
        str(ep["n_leaves"]).encode(), repr(ep["sealed_at"]).encode(),
        ep["sealer_pubkey"].encode(), ep["anchor_type"].encode(),
    ]),

    "US.join same but str(sealed_at)": US.join([
        ep["epoch_id"].encode(), ep["merkle_root"].encode(),
        str(ep["n_leaves"]).encode(), str(ep["sealed_at"]).encode(),
        ep["sealer_pubkey"].encode(), ep["anchor_type"].encode(),
    ]),

    "b2_256(US.join)": b2_256(US.join([
        ep["epoch_id"].encode(), ep["merkle_root"].encode(),
        str(ep["n_leaves"]).encode(), str(ep["sealed_at"]).encode(),
        ep["sealer_pubkey"].encode(), ep["anchor_type"].encode(),
    ])),
}

for name, view in signed_views.items():
    ok = ed_verify(ep["sealer_pubkey"], view, ep["signature"])
    if ok:
        print(f"  ✅ MATCH: {name}")
    else:
        pass  # Don't print all fails

print("\n  (only matches shown)")

# ============================================================================
# Check: maybe signatures are over a HASH of the canonical JSON, not the JSON itself
# ============================================================================
print("\n  Testing: sign(b2_256(canon(...))):")
for key_set in [
    ("epoch_id", "merkle_root", "n_leaves", "sealed_at", "sealer_pubkey", "anchor_type"),
    ("anchor_type", "epoch_id", "merkle_root", "n_leaves", "sealed_at", "sealer_pubkey"),
]:
    d = {k: ep[k] for k in key_set}
    c = canon(d)
    h = b2_256(c)
    ok = ed_verify(ep["sealer_pubkey"], h, ep["signature"])
    if ok:
        print(f"    ✅ MATCH: b2_256(canon({key_set})) — keys in given order")
