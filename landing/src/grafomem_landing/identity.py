"""Ed25519 + TierGate + GEIANT delegation.

Reference only. In production these reuse src/aml/provenance.py (Ed25519/hashing
already in-platform via `cryptography>=41`) and the GNS Identity GEIANT module for
the human-rooted Delegation Certificate + TierGate trust tiers.
"""
import time
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.exceptions import InvalidSignature
from .hashing import canon, b2_128

# ---------- Ed25519 ----------
def gen_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()

def pub_hex(priv: Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

def sign_hex(priv: Ed25519PrivateKey, msg: bytes) -> str:
    return priv.sign(msg).hex()

def verify_hex(pubkey_hex: str, sig_hex: str, msg: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex)).verify(bytes.fromhex(sig_hex), msg)
        return True
    except (InvalidSignature, ValueError):
        return False

# ---------- TierGate ----------
TIERS = ["none", "read", "operate", "curate", "deploy_prep", "release", "admin"]
def tier_rank(t: str) -> int:
    return TIERS.index(t) if t in TIERS else -1

# ---------- GEIANT delegation (rooted in a human principal) ----------
_DELEG_FIELDS = ["agent_handle", "human_principal", "human_pubkey", "tier", "issued_at", "deleg_id"]

def issue_delegation(agent_handle: str, human_principal: str, human_priv: Ed25519PrivateKey, tier: str) -> dict:
    body = {"agent_handle": agent_handle, "human_principal": human_principal,
            "human_pubkey": pub_hex(human_priv), "tier": tier, "issued_at": time.time()}
    body["deleg_id"] = b2_128("deleg", agent_handle, human_principal, tier, str(body["issued_at"]))
    body["signature"] = sign_hex(human_priv, canon({k: body[k] for k in _DELEG_FIELDS}))
    return body

def verify_delegation(d: dict) -> bool:
    """The human principal signed this delegation -> authority roots in a human."""
    return verify_hex(d["human_pubkey"], d["signature"], canon({k: d[k] for k in _DELEG_FIELDS}))
