"""
GRAFOMEM gcrumbs — breadcrumb chain + Merkle epoch anchoring.

Two layers, one name:

  Layer 1 (chain):  Append-only breadcrumb chain.  Each event across R1–R5 +
      erasure emits a breadcrumb carrying the governance decision as payload.
      Chained via ``prev_id`` (raw breadcrumb_id of predecessor), signed with
      Ed25519.  Genesis prev_id = ``'0' * 32``.

  Layer 2 (epoch):  Cumulative-prefix Merkle snapshots over the chain.
      ``roll_epoch()`` seals breadcrumbs [0 : n] into a Merkle root (hex-string
      concatenation tree, NOT R2's domain-separated tree), signs the epoch_id,
      and persists.  Epochs are checkpoints, not partitions.

Ported from the reference implementation:
  landing/src/grafomem_landing/crumbs.py   (Crumbs class)
  landing/src/grafomem_landing/hashing.py  (canon, b2_256, b2_128, US)

B0 (tests/test_gcrumbs_b0.py) reproduces landing/conformance/artifacts/
phase1_gcrumbs_chain.json using the EXACT functions below — any change that
breaks B0 means production diverges from the CDP conformance artifact.

Backed by PostgreSQL via psycopg v3 (sync).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

logger = logging.getLogger("grafomem.cloud.gcrumbs")

# ============================================================================
# Payload Content Guard
# ============================================================================

_FORBIDDEN_PAYLOAD_KEYS = {"content", "raw_output", "query", "input_text", "system_prompt"}

def _validate_payload_no_content(payload: dict) -> None:
    """Reject payloads that embed tenant content in the unencrypted audit trail."""
    for key in _FORBIDDEN_PAYLOAD_KEYS:
        if key in payload:
            raise ValueError(f"Breadcrumb payload must not contain '{key}' — use fact_ref instead")

# ============================================================================
# Hashing primitives (identical to landing/src/grafomem_landing/hashing.py)
# ============================================================================

US = b"\x1f"  # ASCII unit separator


def canon(obj) -> bytes:
    """Deterministic, sorted, separator-stable JSON encoding for hashing/signing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def b2_256(data: bytes) -> str:
    """Content / Merkle-node hash — BLAKE2b-256, returned as 64-char hex."""
    return hashlib.blake2b(data, digest_size=32).hexdigest()


def b2_128(*parts: str) -> str:
    """Identity hash — BLAKE2b-128, 0x1F-separated, returned as 32-char hex.

    Identical to landing/src/grafomem_landing/hashing.py:b2_128.
    """
    h = hashlib.blake2b(digest_size=16)
    h.update(US.join(p.encode() for p in parts))
    return h.hexdigest()


# ============================================================================
# Merkle tree (hex-string concatenation — NOT R2's domain-separated tree)
#
# Identical to landing/src/grafomem_landing/crumbs.py:Crumbs._merkle.
# Node hash = b2_256((left_hex + right_hex).encode()).
# ============================================================================

def _merkle(leaves: list[str]) -> tuple[str, list[list[str]]]:
    """Build a Merkle tree over hex-string leaves.

    Returns (root_hex, levels) where levels[0] = leaves, levels[-1] = [root].
    Odd-count levels duplicate the last element.

    >>> _merkle([])[0] == '0' * 64
    True
    """
    if not leaves:
        return ("0" * 64, [["0" * 64]])
    levels: list[list[str]] = [leaves[:]]
    cur = leaves[:]
    while len(cur) > 1:
        nxt = []
        for i in range(0, len(cur), 2):
            left = cur[i]
            right = cur[i + 1] if i + 1 < len(cur) else cur[i]
            nxt.append(b2_256((left + right).encode()))
        levels.append(nxt)
        cur = nxt
    return (cur[0], levels)


def _leaf(bc: dict) -> str:
    """Merkle leaf for a breadcrumb: b2_256(canon({seq, event_type, payload, prev_id})).

    Identical to landing/src/grafomem_landing/crumbs.py:Crumbs._leaf.
    Uses the FULL payload — if payload was read from JSONB, use the stored
    ``payload_canon`` bytes instead to avoid float round-trip drift.
    """
    return b2_256(canon({k: bc[k] for k in ["seq", "event_type", "payload", "prev_id"]}))


def _leaf_from_row(row: dict) -> str:
    """Leaf from a DB row — uses ``payload_canon`` (raw bytes) to avoid JSONB drift."""
    payload_bytes = row.get("payload_canon")
    if payload_bytes and isinstance(payload_bytes, (bytes, memoryview)):
        # Reconstruct the exact canonical dict that was hashed at append time
        payload = json.loads(payload_bytes)
    else:
        payload = row["payload"]
    return b2_256(canon({
        "seq": row["seq"],
        "event_type": row["event_type"],
        "payload": payload,
        "prev_id": row["prev_id"],
    }))


def verify_inclusion(leaf: str, proof: list[dict], root: str) -> bool:
    """Verify a Merkle inclusion proof.

    proof = [{"hash": sibling_hex, "right": bool}, ...]
    Identical to landing/src/grafomem_landing/crumbs.py:Crumbs.verify_inclusion.
    """
    h = leaf
    for step in proof:
        if step["right"]:
            h = b2_256((h + step["hash"]).encode())
        else:
            h = b2_256((step["hash"] + h).encode())
    return h == root


# ============================================================================
# Schema
# ============================================================================

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS gcrumbs_breadcrumbs (
    breadcrumb_id  TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    seq            BIGINT NOT NULL,
    event_type     TEXT NOT NULL,
    payload        JSONB NOT NULL,
    payload_hash   TEXT NOT NULL,
    payload_canon  BYTEA NOT NULL,
    prev_id        TEXT NOT NULL,
    signature      TEXT NOT NULL,
    signer_pubkey  TEXT NOT NULL,
    source_type    TEXT,
    source_ref     TEXT,
    created_at     DOUBLE PRECISION NOT NULL,
    UNIQUE (tenant_id, seq)
);
CREATE INDEX IF NOT EXISTS gcrumbs_bc_tenant_seq
    ON gcrumbs_breadcrumbs(tenant_id, seq);

CREATE TABLE IF NOT EXISTS gcrumbs_epochs (
    epoch_id       TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    epoch_number   INTEGER NOT NULL,
    merkle_root    TEXT NOT NULL,
    n_leaves       INTEGER NOT NULL,
    sealed_at      DOUBLE PRECISION NOT NULL,
    anchor_type    TEXT NOT NULL DEFAULT 'self-sealed (RFC3161/countersignature/ledger optional)',
    document       JSONB NOT NULL,
    signature      TEXT NOT NULL,
    sealer_pubkey  TEXT NOT NULL,
    UNIQUE (tenant_id, epoch_number)
);
"""

ANCHOR_DEFAULT = "self-sealed (RFC3161/countersignature/ledger optional)"


# ============================================================================
# Errors
# ============================================================================

class GcrumbsError(Exception):
    """Base error for gcrumbs operations."""


# ============================================================================
# GcrumbsService
# ============================================================================

class GcrumbsService:
    """Breadcrumb chain + Merkle epoch anchor.

    Parameters
    ----------
    db_url : str
        PostgreSQL connection URI.
    signing_key : bytes, optional
        32-byte Ed25519 private seed (same key used by R1–R5 services).
    """

    def __init__(self, db_url: str, *, signing_identity=None, pool=None) -> None:
        self.db_url = db_url
        self.signing_identity = signing_identity
        self._pool = pool

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> psycopg.Connection[dict[str, Any]]:
        """Autocommit connection for reads."""
        if self._pool is not None:
            return self._pool.getconn()
        return psycopg.connect(self.db_url, row_factory=dict_row, autocommit=True)

    def _tx_conn(self) -> psycopg.Connection[dict[str, Any]]:
        """Transactional connection for writes."""
        if self._pool is not None:
            conn = self._pool.getconn()
            conn.autocommit = False
            return conn
        return psycopg.connect(self.db_url, row_factory=dict_row, autocommit=False)

    def _put_conn(self, conn):
        if self._pool is not None:
            self._pool.putconn(conn)

    def ensure_schema(self) -> None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(_SCHEMA_SQL)
        finally:
            self._put_conn(conn)
        logger.info("gcrumbs schema ensured")

    # ------------------------------------------------------------------
    # Signing helpers (uses aml.provenance — same as all services)
    # ------------------------------------------------------------------

    def _sign(self, message: bytes) -> tuple[str, str]:
        """Sign message with Ed25519. Returns (signature_hex, pubkey_hex)."""
        if not self.signing_identity:
            raise GcrumbsError("signing_identity required for gcrumbs operations")
        from aml.provenance import sign_provenance
        sig, pub = sign_provenance(self.signing_identity, message)
        return sig.hex(), pub.hex()

    def _pub_hex(self) -> str:
        """Get the public key hex for the signing identity."""
        if not self.signing_identity:
            raise GcrumbsError("signing_identity required")
        return self.signing_identity.public_key().hex()

    # ------------------------------------------------------------------
    # Breadcrumb chain (Layer 1)
    # ------------------------------------------------------------------

    def append_breadcrumb(
        self,
        tenant_id: str,
        event_type: str,
        payload: dict,
        *,
        source_type: str | None = None,
        source_ref: str | None = None,
        conn: psycopg.Connection[dict[str, Any]] | None = None,
    ) -> dict:
        """Append one breadcrumb to the tenant's chain.

        Best-effort from caller's perspective (R1–R5 wrap in try/except).
        Seq + prev_id allocation is atomic (advisory lock).

        Parameters
        ----------
        event_type : str
            Event type matching the CDP vocabulary:
            'action:<name>:ok', 'customs:seal', 'landing_certificate',
            'erasure:issued', 'composition', etc.
        payload : dict
            The governance decision: {args, authorized, reasons, agent, tier}.
        """
        _validate_payload_no_content(payload)
        payload_canon_bytes = canon(payload)
        payload_hash = b2_256(payload_canon_bytes)

        managed_conn = False
        if conn is None:
            conn = self._tx_conn()
            managed_conn = True

        try:
            with conn.cursor() as cur:
                # Per-tenant advisory lock — prevents concurrent seq collision
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"gcrumbs:{tenant_id}",),
                )

                # Find previous breadcrumb
                cur.execute(
                    "SELECT breadcrumb_id, seq FROM gcrumbs_breadcrumbs "
                    "WHERE tenant_id = %s ORDER BY seq DESC LIMIT 1",
                    (tenant_id,),
                )
                row = cur.fetchone()
                seq = (row["seq"] + 1) if row else 0
                prev_id = row["breadcrumb_id"] if row else ("0" * 32)

                # Compute breadcrumb_id — identical to reference
                bid = b2_128(str(seq), event_type, payload_hash, prev_id)

                # Sign breadcrumb_id bytes
                sig_hex, pub_hex = self._sign(bytes.fromhex(bid))
                created_at = time.time()

                cur.execute(
                    "INSERT INTO gcrumbs_breadcrumbs "
                    "(breadcrumb_id, tenant_id, seq, event_type, payload, payload_hash, "
                    " payload_canon, prev_id, signature, signer_pubkey, "
                    " source_type, source_ref, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        bid, tenant_id, seq, event_type,
                        Jsonb(payload), payload_hash, payload_canon_bytes,
                        prev_id, sig_hex, pub_hex,
                        source_type, source_ref, created_at,
                    ),
                )
                if managed_conn:
                    conn.commit()
        finally:
            if managed_conn:
                self._put_conn(conn)

        logger.info(
            "gcrumbs: breadcrumb seq=%d type=%s tenant=%s",
            seq, event_type, tenant_id[:12],
        )
        return {
            "breadcrumb_id": bid, "seq": seq, "prev_id": prev_id,
            "event_type": event_type, "payload_hash": payload_hash,
        }

    # ------------------------------------------------------------------
    # Merkle epochs (Layer 2)
    # ------------------------------------------------------------------

    def roll_epoch(self, tenant_id: str) -> dict:
        """Seal the current chain into a Merkle epoch checkpoint.

        Cumulative prefix: the epoch covers ALL breadcrumbs [0 : n], not
        just new ones since the last epoch.  Epochs are snapshots.

        Transaction-locked to prevent concurrent rolls.
        """
        conn = self._tx_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"gcrumbs-epoch:{tenant_id}",),
                )

                # Read ALL breadcrumbs ordered by seq
                cur.execute(
                    "SELECT breadcrumb_id, seq, event_type, payload, "
                    "       payload_canon, prev_id "
                    "FROM gcrumbs_breadcrumbs "
                    "WHERE tenant_id = %s ORDER BY seq ASC",
                    (tenant_id,),
                )
                rows = cur.fetchall()
                if not rows:
                    raise GcrumbsError("empty epoch: no breadcrumbs to seal")

                # Build Merkle tree
                leaves = [_leaf_from_row(r) for r in rows]
                root, _ = _merkle(leaves)
                n_leaves = len(leaves)

                # Next epoch number
                cur.execute(
                    "SELECT COALESCE(MAX(epoch_number), 0) AS max_n "
                    "FROM gcrumbs_epochs WHERE tenant_id = %s",
                    (tenant_id,),
                )
                epoch_number = cur.fetchone()["max_n"] + 1

                # Seal
                sealed_at = time.time()
                epoch_id = b2_128("epoch", root, str(sealed_at))
                pub_hex = self._pub_hex()

                document = {
                    "epoch_id": epoch_id,
                    "merkle_root": root,
                    "n_leaves": n_leaves,
                    "sealed_at": sealed_at,
                    "sealer_pubkey": pub_hex,
                    "anchor_type": ANCHOR_DEFAULT,
                }

                # Sign epoch_id bytes (not the document)
                sig_hex, _ = self._sign(bytes.fromhex(epoch_id))

                cur.execute(
                    "INSERT INTO gcrumbs_epochs "
                    "(epoch_id, tenant_id, epoch_number, merkle_root, n_leaves, "
                    " sealed_at, anchor_type, document, signature, sealer_pubkey) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        epoch_id, tenant_id, epoch_number, root, n_leaves,
                        sealed_at, ANCHOR_DEFAULT, Jsonb(document),
                        sig_hex, pub_hex,
                    ),
                )
                conn.commit()
        finally:
            self._put_conn(conn)

        logger.info(
            "gcrumbs: epoch %d sealed (%d leaves) tenant=%s",
            epoch_number, n_leaves, tenant_id[:12],
        )
        return document | {"signature": sig_hex, "epoch_number": epoch_number}

    # ------------------------------------------------------------------
    # Inclusion proofs
    # ------------------------------------------------------------------

    def inclusion_proof(
        self, tenant_id: str, epoch_number: int, breadcrumb_seq: int,
    ) -> dict:
        """Merkle inclusion proof for a breadcrumb within a sealed epoch."""
        # Get epoch
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM gcrumbs_epochs "
                    "WHERE tenant_id = %s AND epoch_number = %s",
                    (tenant_id, epoch_number),
                )
                epoch = cur.fetchone()
        finally:
            self._put_conn(conn)
        if not epoch:
            raise GcrumbsError(f"epoch {epoch_number} not found")

        n_leaves = epoch["n_leaves"]
        if breadcrumb_seq >= n_leaves:
            return {"included": False, "reason": "breadcrumb not in this epoch"}

        # Rebuild tree
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT breadcrumb_id, seq, event_type, payload, "
                    "       payload_canon, prev_id "
                    "FROM gcrumbs_breadcrumbs "
                    "WHERE tenant_id = %s AND seq < %s ORDER BY seq ASC",
                    (tenant_id, n_leaves),
                )
                rows = cur.fetchall()
        finally:
            self._put_conn(conn)

        leaves = [_leaf_from_row(r) for r in rows]
        _, levels = _merkle(leaves)

        # Build proof path
        idx = breadcrumb_seq
        proof = []
        for level in levels[:-1]:
            sib = idx ^ 1
            sib_hash = level[sib] if sib < len(level) else level[idx]
            proof.append({"hash": sib_hash, "right": (idx % 2 == 0)})
            idx //= 2

        return {
            "included": True,
            "leaf": leaves[breadcrumb_seq],
            "proof": proof,
            "merkle_root": epoch["merkle_root"],
            "epoch_number": epoch_number,
            "breadcrumb_seq": breadcrumb_seq,
        }

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    def verify_chain(self, tenant_id: str) -> dict:
        """Full verification: chain linkage + payload hashes + signatures + epoch roots."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT breadcrumb_id, seq, event_type, payload, "
                    "       payload_hash, payload_canon, prev_id, "
                    "       signature, signer_pubkey "
                    "FROM gcrumbs_breadcrumbs "
                    "WHERE tenant_id = %s ORDER BY seq ASC",
                    (tenant_id,),
                )
                bcs = cur.fetchall()

                cur.execute(
                    "SELECT * FROM gcrumbs_epochs "
                    "WHERE tenant_id = %s ORDER BY epoch_number ASC",
                    (tenant_id,),
                )
                epochs = cur.fetchall()
        finally:
            self._put_conn(conn)

        if not bcs:
            return {"status": "empty", "breadcrumbs_verified": 0, "epochs_verified": 0}

        # Verify breadcrumb chain
        prev = "0" * 32
        for bc in bcs:
            # Payload hash
            canon_bytes = bc.get("payload_canon")
            if canon_bytes and isinstance(canon_bytes, (bytes, memoryview)):
                payload_for_hash = bytes(canon_bytes)
            else:
                payload_for_hash = canon(bc["payload"])
            if b2_256(payload_for_hash) != bc["payload_hash"]:
                return {
                    "status": "tampered", "breadcrumbs_verified": bc["seq"],
                    "reason": "payload_hash_mismatch", "at_seq": bc["seq"],
                }

            # Chain linkage
            if bc["prev_id"] != prev:
                return {
                    "status": "tampered", "breadcrumbs_verified": bc["seq"],
                    "reason": "chain_broken", "at_seq": bc["seq"],
                }

            # Breadcrumb ID
            expected_bid = b2_128(
                str(bc["seq"]), bc["event_type"], bc["payload_hash"], bc["prev_id"],
            )
            if bc["breadcrumb_id"] != expected_bid:
                return {
                    "status": "tampered", "breadcrumbs_verified": bc["seq"],
                    "reason": "breadcrumb_id_mismatch", "at_seq": bc["seq"],
                }

            # Signature
            try:
                from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                    Ed25519PublicKey,
                )
                pub = Ed25519PublicKey.from_public_bytes(
                    bytes.fromhex(bc["signer_pubkey"]),
                )
                pub.verify(
                    bytes.fromhex(bc["signature"]),
                    bytes.fromhex(bc["breadcrumb_id"]),
                )
            except Exception:
                return {
                    "status": "tampered", "breadcrumbs_verified": bc["seq"],
                    "reason": "signature_invalid", "at_seq": bc["seq"],
                }

            prev = bc["breadcrumb_id"]

        # Verify epochs
        for ep in epochs:
            n = ep["n_leaves"]
            leaves = [_leaf_from_row(bc) for bc in bcs[:n]]
            root, _ = _merkle(leaves)
            if root != ep["merkle_root"]:
                return {
                    "status": "tampered", "breadcrumbs_verified": len(bcs),
                    "reason": "epoch_root_mismatch", "epoch_number": ep["epoch_number"],
                }

            # Epoch signature
            try:
                from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                    Ed25519PublicKey,
                )
                pub = Ed25519PublicKey.from_public_bytes(
                    bytes.fromhex(ep["sealer_pubkey"]),
                )
                pub.verify(
                    bytes.fromhex(ep["signature"]),
                    bytes.fromhex(ep["epoch_id"]),
                )
            except Exception:
                return {
                    "status": "tampered", "breadcrumbs_verified": len(bcs),
                    "reason": "epoch_signature_invalid",
                    "epoch_number": ep["epoch_number"],
                }

        return {
            "status": "intact",
            "breadcrumbs_verified": len(bcs),
            "epochs_verified": len(epochs),
        }

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_breadcrumbs(
        self, tenant_id: str, limit: int = 100, offset: int = 0,
    ) -> list[dict]:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT breadcrumb_id, seq, event_type, payload, payload_hash, "
                    "       prev_id, signature, signer_pubkey, source_type, "
                    "       source_ref, created_at "
                    "FROM gcrumbs_breadcrumbs "
                    "WHERE tenant_id = %s ORDER BY seq ASC LIMIT %s OFFSET %s",
                    (tenant_id, limit, offset),
                )
                return cur.fetchall()
        finally:
            self._put_conn(conn)

    def get_epochs(self, tenant_id: str) -> list[dict]:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM gcrumbs_epochs "
                    "WHERE tenant_id = %s ORDER BY epoch_number ASC",
                    (tenant_id,),
                )
                return cur.fetchall()
        finally:
            self._put_conn(conn)

    def get_epoch(self, tenant_id: str, epoch_number: int) -> dict | None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM gcrumbs_epochs "
                    "WHERE tenant_id = %s AND epoch_number = %s",
                    (tenant_id, epoch_number),
                )
                return cur.fetchone()
        finally:
            self._put_conn(conn)

    def get_latest_epoch(self, tenant_id: str) -> dict | None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM gcrumbs_epochs "
                    "WHERE tenant_id = %s ORDER BY epoch_number DESC LIMIT 1",
                    (tenant_id,),
                )
                return cur.fetchone()
        finally:
            self._put_conn(conn)

    def get_stats(self, tenant_id: str) -> dict:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS total_breadcrumbs FROM gcrumbs_breadcrumbs "
                    "WHERE tenant_id = %s",
                    (tenant_id,),
                )
                bc_count = cur.fetchone()["total_breadcrumbs"]
                cur.execute(
                    "SELECT COUNT(*) AS total_epochs FROM gcrumbs_epochs "
                    "WHERE tenant_id = %s",
                    (tenant_id,),
                )
                ep_count = cur.fetchone()["total_epochs"]
        finally:
            self._put_conn(conn)
        return {"total_breadcrumbs": bc_count, "total_epochs": ep_count}
