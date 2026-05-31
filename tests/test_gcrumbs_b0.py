"""
B0 — Reproduce phase1_gcrumbs_chain.json using production gcrumbs functions.

This is the single most important gcrumbs test:
 - It imports _leaf, _merkle, b2_128, b2_256, canon, verify_inclusion
   FROM src/aml/cloud/gcrumbs.py (production code).
 - It reproduces the live CDP conformance artifact byte-for-byte.
 - It requires NO database — pure crypto verification.
 - It verifies recorded signatures, it does NOT re-sign.

If B0 is green, production is correct by construction. If B0 fails,
production has diverged from the CDP artifact.

Run:  pytest tests/test_gcrumbs_b0.py -v
"""
import json
import os
import sys

import pytest

# Ensure src is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aml.cloud.gcrumbs import (  # noqa: E402
    _leaf,
    _merkle,
    b2_128,
    b2_256,
    canon,
    verify_inclusion,
)

# ---- Load the CDP artifact ----

ARTIFACT_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "landing",
    "conformance",
    "artifacts",
    "phase1_gcrumbs_chain.json",
)


@pytest.fixture(scope="module")
def artifact():
    with open(ARTIFACT_PATH) as f:
        return json.load(f)


@pytest.fixture(scope="module")
def breadcrumbs(artifact):
    return sorted(artifact["breadcrumbs"], key=lambda b: b["seq"])


@pytest.fixture(scope="module")
def epoch1(artifact):
    return artifact["epoch1"]


@pytest.fixture(scope="module")
def epoch2(artifact):
    return artifact["epoch2"]


@pytest.fixture(scope="module")
def sealer_pubkey(epoch1):
    return epoch1["sealer_pubkey"]


def _ed_verify(pubkey_hex: str, message: bytes, sig_hex: str) -> bool:
    """Verify Ed25519 signature. B0 verifies, it does NOT re-sign."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
    pub.verify(bytes.fromhex(sig_hex), message)
    return True


# ============================================================================
# B0 Tests
# ============================================================================


class TestB0ReproduceCDPArtifact:
    """Reproduce phase1_gcrumbs_chain.json using production gcrumbs functions."""

    def test_b0_01_chain_linkage(self, breadcrumbs):
        """Genesis prev_id = '0'*32, then each prev_id = prior breadcrumb_id."""
        prev = "0" * 32
        for bc in breadcrumbs:
            assert bc["prev_id"] == prev, (
                f"chain break at seq {bc['seq']}: "
                f"prev_id={bc['prev_id'][:16]}... expected={prev[:16]}..."
            )
            prev = bc["breadcrumb_id"]

    def test_b0_02_payload_hash(self, breadcrumbs):
        """payload_hash = b2_256(canon(payload))."""
        for bc in breadcrumbs:
            computed = b2_256(canon(bc["payload"]))
            assert computed == bc["payload_hash"], (
                f"payload_hash mismatch at seq {bc['seq']}"
            )

    def test_b0_03_breadcrumb_id(self, breadcrumbs):
        """breadcrumb_id = b2_128(str(seq), event_type, payload_hash, prev_id)."""
        for bc in breadcrumbs:
            computed = b2_128(
                str(bc["seq"]), bc["event_type"],
                bc["payload_hash"], bc["prev_id"],
            )
            assert computed == bc["breadcrumb_id"], (
                f"breadcrumb_id mismatch at seq {bc['seq']}: "
                f"got={computed[:20]}... expected={bc['breadcrumb_id'][:20]}..."
            )

    def test_b0_04_breadcrumb_signatures(self, breadcrumbs, sealer_pubkey):
        """Ed25519 signatures verified (sign over breadcrumb_id bytes)."""
        for bc in breadcrumbs:
            assert _ed_verify(
                sealer_pubkey,
                bytes.fromhex(bc["breadcrumb_id"]),
                bc["signature"],
            ), f"signature failed at seq {bc['seq']}"

    def test_b0_05_merkle_root_epoch1(self, breadcrumbs, epoch1):
        """Epoch 1 Merkle root (20 leaves, cumulative prefix)."""
        leaves = [_leaf(bc) for bc in breadcrumbs[: epoch1["n_leaves"]]]
        root, _ = _merkle(leaves)
        assert root == epoch1["merkle_root"], (
            f"epoch1 root mismatch: got={root[:24]}... "
            f"expected={epoch1['merkle_root'][:24]}..."
        )

    def test_b0_06_merkle_root_epoch2(self, breadcrumbs, epoch2):
        """Epoch 2 Merkle root (22 leaves, cumulative prefix)."""
        leaves = [_leaf(bc) for bc in breadcrumbs[: epoch2["n_leaves"]]]
        root, _ = _merkle(leaves)
        assert root == epoch2["merkle_root"], (
            f"epoch2 root mismatch: got={root[:24]}... "
            f"expected={epoch2['merkle_root'][:24]}..."
        )

    def test_b0_07_epoch_signatures(self, epoch1, epoch2):
        """Epoch signatures (Ed25519 over epoch_id bytes)."""
        assert _ed_verify(
            epoch1["sealer_pubkey"],
            bytes.fromhex(epoch1["epoch_id"]),
            epoch1["signature"],
        ), "epoch1 signature failed"

        assert _ed_verify(
            epoch2["sealer_pubkey"],
            bytes.fromhex(epoch2["epoch_id"]),
            epoch2["signature"],
        ), "epoch2 signature failed"

    def test_b0_08_cumulative_membership(self, breadcrumbs, epoch1, epoch2):
        """Epochs are cumulative prefixes (not disjoint partitions)."""
        n1, n2 = epoch1["n_leaves"], epoch2["n_leaves"]
        total = len(breadcrumbs)

        # Sum exceeds breadcrumb count → cumulative, not disjoint
        assert n1 + n2 > total, (
            f"not cumulative: {n1} + {n2} = {n1 + n2} <= {total}"
        )
        # Epoch 2 includes epoch 1's range
        assert n2 > n1, f"epoch2 ({n2}) should cover more than epoch1 ({n1})"
        # Unsaved tail exists
        assert total > n2, f"expected unsealed tail after epoch2"

    def test_b0_09_inclusion_proof(self, breadcrumbs, epoch2):
        """Inclusion proof: rebuild tree, extract proof for leaf 0, verify."""
        n = epoch2["n_leaves"]
        leaves = [_leaf(bc) for bc in breadcrumbs[:n]]
        root, levels = _merkle(leaves)

        # Build proof for index 0
        idx = 0
        proof = []
        for level in levels[:-1]:
            sib = idx ^ 1
            sib_hash = level[sib] if sib < len(level) else level[idx]
            proof.append({"hash": sib_hash, "right": (idx % 2 == 0)})
            idx //= 2

        # Verify using production verify_inclusion
        assert verify_inclusion(leaves[0], proof, root), (
            "inclusion proof failed for leaf 0"
        )
        assert len(proof) > 0, "proof must be non-vacuous"

        # Also test a middle leaf
        mid = n // 2
        idx = mid
        proof_mid = []
        for level in levels[:-1]:
            sib = idx ^ 1
            sib_hash = level[sib] if sib < len(level) else level[idx]
            proof_mid.append({"hash": sib_hash, "right": (idx % 2 == 0)})
            idx //= 2

        assert verify_inclusion(leaves[mid], proof_mid, root), (
            f"inclusion proof failed for leaf {mid}"
        )
