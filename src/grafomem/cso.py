"""The CSO and the .gfm codec (SPEC-1.0 §1, typed header)."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json, hashlib, struct
import numpy as np
from .contracts import read

import re
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.exceptions import InvalidSignature
from grafomem.errors import SignatureMismatch, UnknownKey

GFM_MAGIC = b"GFM1"
GFM_VERSION = "1.2"
SIG_ALG = "ed25519"

def _default_consent(): return {"subject_id": None, "policy": "private", "expires_at": None}

def _validate_typed_header(h: dict):
    consent = h.get("consent", {})
    if consent.get("policy") not in {"private", "tenant", "public"}:
        raise ValueError(f"Invalid consent policy: {consent.get('policy')}")
    exp = consent.get("expires_at")
    if exp is not None:
        try:
            dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if dt <= datetime.now(timezone.utc):
                raise ValueError("Consent expired")
        except ValueError as e:
            if "Consent expired" in str(e):
                raise
            raise ValueError(f"Invalid expires_at format: {e}")
    meta = h.get("meta", {})
    ver = meta.get("version")
    if not ver or not re.match(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$", ver):
        raise ValueError(f"Invalid meta.version semver: {ver}")
    for cap in h.get("capabilities", []):
        if not re.match(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+$", cap):
            raise ValueError(f"Invalid capability format: {cap}")

@dataclass(eq=False)
class CSO:
    """Cognitive State Object — the linkable unit, read as y = M q. (identity equality)"""
    M: np.ndarray
    model_id: str
    capabilities: frozenset = frozenset()
    consent: dict = field(default_factory=_default_consent)
    alpha: float = 0.0
    layout: str = "row-major"
    meta: dict = field(default_factory=lambda: {"provenance": None, "version": "1.0.0", "tags": []})
    key_id: str | None = None
    payload_type: str = "tensor"
    blob: bytes | None = None

    @property
    def d(self) -> int: return int(self.M.shape[0]) if self.M is not None else 0
    def read(self, q, act="identity"):
        if self.payload_type == "blob":
            raise TypeError("Cannot read() from a blob CSO. y=Mq is undefined for inert envelopes.")
        return read(self.M, q, act)

    def header(self) -> dict:
        # The WIRE header — signed byte-for-byte. Includes signer/format fields (key_id,
        # sig_alg, gfm_version) so the signature binds them.
        return {"gfm_version": GFM_VERSION, "sig_alg": SIG_ALG, "model_id": self.model_id, "d": self.d,
                "dtype": "float32", "alpha": self.alpha, "layout": self.layout,
                "capabilities": sorted(self.capabilities), "consent": self.consent, "meta": self.meta,
                "key_id": self.key_id, "payload_type": self.payload_type}

    def _identity(self) -> dict:
        # Content identity for addressing — EXCLUDES signer/format fields (key_id, sig_alg,
        # gfm_version) and provenance, so the same state signed by two keys hashes equally.
        meta = {k: v for k, v in self.meta.items() if k != "provenance"}
        return {"model_id": self.model_id, "d": self.d, "dtype": "float32",
                "alpha": self.alpha, "layout": self.layout,
                "capabilities": sorted(self.capabilities), "consent": self.consent, "meta": meta,
                "payload_type": self.payload_type}

    def content_hash(self) -> str:
        payload = self.M.astype("<f4").tobytes() if self.payload_type == "tensor" and self.M is not None else (self.blob or b"")
        return hashlib.sha256(json.dumps(self._identity(), sort_keys=True).encode() + payload).hexdigest()

    def consent_valid(self) -> bool:
        exp = self.consent.get("expires_at")
        if exp is None: return True
        try: return datetime.fromisoformat(exp.replace("Z", "+00:00")) > datetime.now(timezone.utc)
        except Exception: return False

    def to_gfm(self, private_key: ed25519.Ed25519PrivateKey) -> bytes:
        hdr = json.dumps(self.header()).encode()
        payload = self.M.astype("<f4").tobytes() if self.payload_type == "tensor" and self.M is not None else (self.blob or b"")
        sig = private_key.sign(hdr + payload)
        return GFM_MAGIC + struct.pack("<I", len(hdr)) + hdr + struct.pack("<I", len(payload)) + payload + sig

    @staticmethod
    def from_gfm(b: bytes, trusted_keys: dict[str, ed25519.Ed25519PublicKey]) -> "CSO":
        if len(b) < 4 or b[:4] != GFM_MAGIC: raise ValueError("bad magic")
        o = 4
        if len(b) < o + 4: raise ValueError("truncated buffer (header length)")
        (hl,) = struct.unpack("<I", b[o:o+4]); o += 4
        if len(b) < o + hl: raise ValueError("truncated buffer (header)")
        hdr_bytes = b[o:o+hl]; o += hl
        if len(b) < o + 4: raise ValueError("truncated buffer (tensor length)")
        (tl,) = struct.unpack("<I", b[o:o+4]); o += 4
        if len(b) < o + tl: raise ValueError("truncated buffer (tensor)")
        tensor = b[o:o+tl]; o += tl
        sig = b[o:]
        if len(sig) != 64: raise ValueError(f"invalid signature length: {len(sig)}")

        try: h = json.loads(hdr_bytes)
        except json.JSONDecodeError: raise ValueError("malformed header json")

        # --- AUTHENTICATE BEFORE INTERPRETING: never run semantic validation on unverified bytes ---
        sig_alg = h.get("sig_alg", "ed25519")
        if sig_alg != SIG_ALG: raise ValueError(f"unsupported sig_alg: {sig_alg}")
        key_id = h.get("key_id")
        pub_key = trusted_keys.get(key_id)
        if not pub_key: raise UnknownKey(f"unknown key_id: {key_id}")
        try: pub_key.verify(sig, hdr_bytes + tensor)
        except InvalidSignature: raise SignatureMismatch("signature mismatch")

        # --- only now interpret the (authenticated) header ---
        _validate_typed_header(h)
        payload_type = h.get("payload_type", "tensor")
        
        M_arr = None
        blob_bytes = None
        
        if payload_type == "tensor":
            expected_tensor_size = h.get("d", 0) * h.get("d", 0) * 4
            if len(tensor) != expected_tensor_size: raise ValueError("tensor length does not match d*d*4")
            M_arr = np.frombuffer(tensor, dtype="<f4").reshape(h["d"], h["d"]).astype(np.float32).copy()
        else:
            blob_bytes = tensor

        return CSO(M=M_arr, model_id=h["model_id"], capabilities=frozenset(h["capabilities"]),
                   consent=h.get("consent", _default_consent()), alpha=h.get("alpha", 0.0),
                   layout=h.get("layout", "row-major"), meta=h.get("meta", {}),
                   key_id=key_id, payload_type=payload_type, blob=blob_bytes)
