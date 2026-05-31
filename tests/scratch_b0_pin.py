#!/usr/bin/env python3
"""
B0 hypothesis tester — run against phase1_gcrumbs_chain.json to pin
the four single-point decisions:

  (a) _breadcrumb_id preimage
  (b) _leaf_preimage for Merkle
  (c) _epoch_leaves membership rule (cumulative prefix vs disjoint)
  (d) _epoch_signed_view (bytes the epoch signature covers)

Run:  python3 tests/scratch_b0_pin.py
"""
import hashlib, json, sys, os

US = b"\x1f"

def b2_128(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=16).digest()

def b2_256(data: bytes) -> bytes:
    return hashlib.blake2b(data, digest_size=32).digest()

def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()

# ---- R2's Merkle tree (verbatim from provenance_customs.py) ----
LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"
EMPTY_PREFIX = b"\x02"

def _m_leaf(data: bytes) -> bytes:
    return hashlib.blake2b(LEAF_PREFIX + data, digest_size=32).digest()

def _m_node(a: bytes, b: bytes) -> bytes:
    return hashlib.blake2b(NODE_PREFIX + a + b, digest_size=32).digest()

def merkle_root(leaves: list[bytes]) -> bytes:
    if not leaves:
        return hashlib.blake2b(EMPTY_PREFIX + b"empty", digest_size=32).digest()
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_m_node(level[i], level[i+1]) for i in range(0, len(level), 2)]
    return level[0]

# ---- Ed25519 verify ----
def ed_verify(pubkey_hex: str, message: bytes, sig_hex: str) -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        pub.verify(bytes.fromhex(sig_hex), message)
        return True
    except Exception as e:
        return False

# ============================================================================
# HYPOTHESIS FUNCTIONS — tune until B0 is green
# ============================================================================

# (a) breadcrumb_id preimage
def _breadcrumb_id_h1(seq, event_type, payload_hash, prev_id):
    """H1: b2_128(US.join(seq, event_type, payload_hash_bytes, prev_id_bytes))"""
    return b2_128(US.join([
        str(seq).encode(),
        event_type.encode(),
        bytes.fromhex(payload_hash),
        bytes.fromhex(prev_id),
    ])).hex()

def _breadcrumb_id_h2(seq, event_type, payload_hash, prev_id):
    """H2: b2_128(US.join(seq, event_type, payload_hash_hex, prev_id_hex)) — hex strings not bytes"""
    return b2_128(US.join([
        str(seq).encode(),
        event_type.encode(),
        payload_hash.encode(),
        prev_id.encode(),
    ])).hex()

def _breadcrumb_id_h3(seq, event_type, payload_hash, prev_id):
    """H3: b2_128(canon({seq, event_type, payload_hash, prev_id}))"""
    return b2_128(canon({
        "seq": seq,
        "event_type": event_type,
        "payload_hash": payload_hash,
        "prev_id": prev_id,
    })).hex()

# (b) leaf preimage for Merkle
def _leaf_preimage_h1(bc):
    """H1: breadcrumb_id bytes"""
    return bytes.fromhex(bc["breadcrumb_id"])

def _leaf_preimage_h2(bc):
    """H2: payload_hash bytes"""
    return bytes.fromhex(bc["payload_hash"])

def _leaf_preimage_h3(bc):
    """H3: canon of full breadcrumb (all artifact fields)"""
    return canon({
        "seq": bc["seq"],
        "event_type": bc["event_type"],
        "payload": bc["payload"],
        "payload_hash": bc["payload_hash"],
        "prev_id": bc["prev_id"],
        "breadcrumb_id": bc["breadcrumb_id"],
        "signature": bc["signature"],
    })

def _leaf_preimage_h4(bc):
    """H4: canon of breadcrumb WITHOUT signature"""
    return canon({
        "seq": bc["seq"],
        "event_type": bc["event_type"],
        "payload": bc["payload"],
        "payload_hash": bc["payload_hash"],
        "prev_id": bc["prev_id"],
        "breadcrumb_id": bc["breadcrumb_id"],
    })

# (d) epoch signed view
def _epoch_signed_view_h1(ep):
    """H1: canon of document excluding signature"""
    return canon({k: ep[k] for k in
                  ("epoch_id", "merkle_root", "n_leaves", "sealed_at", "sealer_pubkey", "anchor_type")})

def _epoch_signed_view_h2(ep):
    """H2: canon of all keys sorted"""
    d = {k: v for k, v in ep.items() if k != "signature"}
    return canon(d)


# ============================================================================
# LOAD ARTIFACT
# ============================================================================

artifact_path = os.path.join(os.path.dirname(__file__), "..",
                             "landing", "conformance", "artifacts", "phase1_gcrumbs_chain.json")
with open(artifact_path) as f:
    art = json.load(f)

bcs = sorted(art["breadcrumbs"], key=lambda b: b["seq"])
epochs = []
for k in sorted(art.keys()):
    if k.startswith("epoch"):
        epochs.append(art[k])

print(f"Loaded: {len(bcs)} breadcrumbs, {len(epochs)} epochs")
print(f"Epoch sizes: {[e['n_leaves'] for e in epochs]}")
print()

# ============================================================================
# TEST 1: Chain linkage (prev_id)
# ============================================================================
print("=" * 60)
print("TEST 1: Chain linkage (prev_id)")
print("=" * 60)
prev = "0" * 32
chain_ok = True
for bc in bcs:
    if bc["prev_id"] != prev:
        print(f"  ❌ seq {bc['seq']}: prev_id={bc['prev_id'][:16]}... expected={prev[:16]}...")
        chain_ok = False
        break
    prev = bc["breadcrumb_id"]
if chain_ok:
    print("  ✅ All 26 breadcrumbs chain correctly. Genesis prev_id = '0'*32")
print()

# ============================================================================
# TEST 2: payload_hash verification
# ============================================================================
print("=" * 60)
print("TEST 2: payload_hash = b2_256(canon(payload))")
print("=" * 60)
ph_ok = 0
ph_fail = 0
for bc in bcs:
    computed = b2_256(canon(bc["payload"])).hex()
    if computed == bc["payload_hash"]:
        ph_ok += 1
    else:
        ph_fail += 1
        if ph_fail <= 3:
            print(f"  ❌ seq {bc['seq']}: computed={computed[:16]}... stored={bc['payload_hash'][:16]}...")
print(f"  {'✅' if ph_fail == 0 else '❌'} {ph_ok}/{len(bcs)} payload_hash verified")
print()

# ============================================================================
# TEST 3: breadcrumb_id preimage (hypothesis testing)
# ============================================================================
print("=" * 60)
print("TEST 3: breadcrumb_id preimage hypotheses")
print("=" * 60)
for name, fn in [("H1: US.join(bytes)", _breadcrumb_id_h1),
                 ("H2: US.join(hex strings)", _breadcrumb_id_h2),
                 ("H3: canon(dict)", _breadcrumb_id_h3)]:
    ok = 0
    first_fail = None
    for bc in bcs:
        computed = fn(bc["seq"], bc["event_type"], bc["payload_hash"], bc["prev_id"])
        if computed == bc["breadcrumb_id"]:
            ok += 1
        elif first_fail is None:
            first_fail = (bc["seq"], computed, bc["breadcrumb_id"])
    status = "✅" if ok == len(bcs) else "❌"
    print(f"  {status} {name}: {ok}/{len(bcs)}")
    if first_fail:
        print(f"      First fail at seq {first_fail[0]}: got={first_fail[1][:20]}... expected={first_fail[2][:20]}...")
print()

# ============================================================================
# TEST 4: Ed25519 signature verification on breadcrumbs
# ============================================================================
print("=" * 60)
print("TEST 4: Ed25519 signatures on breadcrumbs")
print("=" * 60)
# We need to find the pubkey — should be in an epoch
sealer_pubkey = epochs[0]["sealer_pubkey"] if epochs else None
print(f"  Sealer pubkey from epoch1: {sealer_pubkey[:24]}..." if sealer_pubkey else "  ⚠️ No epoch pubkey found")

# Artifact breadcrumbs don't have a signer_pubkey field, so use the epoch's
# The signature is over breadcrumb_id bytes
sig_ok = 0
sig_fail = 0
for bc in bcs:
    if ed_verify(sealer_pubkey, bytes.fromhex(bc["breadcrumb_id"]), bc["signature"]):
        sig_ok += 1
    else:
        sig_fail += 1
        if sig_fail <= 3:
            print(f"  ❌ seq {bc['seq']}: sig verify failed (signing breadcrumb_id bytes)")
print(f"  {'✅' if sig_fail == 0 else '❌'} {sig_ok}/{len(bcs)} breadcrumb signatures verified")
print()

# ============================================================================
# TEST 5: Merkle root — leaf preimage hypotheses
# ============================================================================
print("=" * 60)
print("TEST 5: Merkle root — leaf preimage hypotheses")
print("=" * 60)

for ep_idx, ep in enumerate(epochs):
    n = ep["n_leaves"]
    expected_root = ep["merkle_root"]
    print(f"\n  Epoch {ep_idx+1} ({n} leaves, expected root: {expected_root[:24]}...):")

    for name, fn in [("H1: breadcrumb_id bytes", _leaf_preimage_h1),
                     ("H2: payload_hash bytes", _leaf_preimage_h2),
                     ("H3: canon(full bc with sig)", _leaf_preimage_h3),
                     ("H4: canon(bc without sig)", _leaf_preimage_h4)]:
        # Cumulative prefix hypothesis
        leaves_raw = [fn(bc) for bc in bcs[:n]]
        leaves_hashed = [_m_leaf(l) for l in leaves_raw]
        root = merkle_root(leaves_hashed)

        # Also try without domain separation (raw b2_256)
        leaves_raw_nodomain = [hashlib.blake2b(l, digest_size=32).digest() for l in leaves_raw]
        root_nodomain = merkle_root(leaves_raw_nodomain)

        # Also try feeding raw bytes directly as leaves (no _m_leaf wrapping)
        root_direct = merkle_root(leaves_raw) if all(len(l) == 32 for l in leaves_raw) else b"skip"

        match = root.hex() == expected_root
        match_nd = root_nodomain.hex() == expected_root
        match_direct = root_direct.hex() == expected_root if root_direct != b"skip" else False

        if match:
            print(f"    ✅ {name} (with R2 domain-sep _m_leaf)")
        elif match_nd:
            print(f"    ✅ {name} (with raw b2_256, no domain-sep)")
        elif match_direct:
            print(f"    ✅ {name} (direct as leaves, no hashing)")
        else:
            print(f"    ❌ {name}")
            print(f"       R2 root:    {root.hex()[:24]}...")
            if root_direct != b"skip":
                print(f"       direct root: {root_direct.hex()[:24]}...")

print()

# ============================================================================
# TEST 6: Epoch signature verification
# ============================================================================
print("=" * 60)
print("TEST 6: Epoch Ed25519 signature")
print("=" * 60)
for ep_idx, ep in enumerate(epochs):
    for name, fn in [("H1: canon(6 fields)", _epoch_signed_view_h1),
                     ("H2: canon(all non-sig)", _epoch_signed_view_h2)]:
        view = fn(ep)
        ok = ed_verify(ep["sealer_pubkey"], view, ep["signature"])
        print(f"  Epoch {ep_idx+1} {name}: {'✅' if ok else '❌'}")
        if not ok and name.startswith("H1"):
            print(f"    signed view ({len(view)} bytes): {view[:80]}...")

print()

# ============================================================================
# TEST 7: Epoch membership — cumulative vs disjoint
# ============================================================================
print("=" * 60)
print("TEST 7: Epoch membership analysis")
print("=" * 60)
print(f"  Breadcrumbs: {len(bcs)}")
for ep_idx, ep in enumerate(epochs):
    print(f"  Epoch {ep_idx+1}: n_leaves={ep['n_leaves']}")
total_leaves = sum(e["n_leaves"] for e in epochs)
print(f"  Sum of n_leaves: {total_leaves}")
print(f"  Cumulative? {total_leaves > len(bcs)} (sum > breadcrumb count)")
if len(epochs) >= 2:
    print(f"  Prefix hypothesis: epoch1=seq[0:{epochs[0]['n_leaves']}], epoch2=seq[0:{epochs[1]['n_leaves']}]")
    print(f"  Disjoint hypothesis: epoch1=seq[0:{epochs[0]['n_leaves']}], epoch2=seq[{epochs[0]['n_leaves']}:{epochs[0]['n_leaves']+epochs[1]['n_leaves']-epochs[0]['n_leaves']}]")

# Also test disjoint for Merkle root
print("\n  Disjoint Merkle test:")
for ep_idx, ep in enumerate(epochs):
    if ep_idx == 0:
        subset = bcs[:ep["n_leaves"]]
    else:
        prev_n = epochs[ep_idx-1]["n_leaves"]
        subset = bcs[prev_n:prev_n + (ep["n_leaves"] - prev_n)]

    for name, fn in [("H1: breadcrumb_id bytes", _leaf_preimage_h1)]:
        leaves_raw = [fn(bc) for bc in subset]
        if all(len(l) == 32 for l in leaves_raw):
            leaves_hashed = [_m_leaf(l) for l in leaves_raw]
            root = merkle_root(leaves_hashed)
            root_direct = merkle_root(leaves_raw)
            match = root.hex() == ep["merkle_root"]
            match_d = root_direct.hex() == ep["merkle_root"]
            print(f"    Epoch {ep_idx+1} disjoint({len(subset)} bcs): R2={match}, direct={match_d}")

print()
print("=" * 60)
print("DONE — use passing hypotheses to build gcrumbs.py")
print("=" * 60)
