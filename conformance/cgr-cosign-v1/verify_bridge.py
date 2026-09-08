#!/usr/bin/env python3
"""Bridge template for a `cgr.cosign.v1` verifier.

There is **no cosign verifier yet** — `clients/cgr-verify` (JS) implements attestation
v1–v4 only, and `packages/grafomem-cgr` is the capture client. This corpus is the
executable target a future verifier must meet (spec → corpus → implementation, the v4
order). Until a verifier exists, Layer 2 of `tests/test_cosign_conformance.py` skips.

When a verifier exists, expose a `verify(record, registry, ledger) -> dict` and point
`CGR_COSIGN_VERIFIER` at it (this file, or an importable module). The contract, per
`docs/cgr/cgr-cosign-v1-spec.md` §8 (amended by decision 0010):

    verify(record: dict, registry: dict, ledger: dict) -> {
        "valid":   bool,                 # the §8 verdict
        "reason":  str,                  # on reject: MUST contain the vector's reason_contains
                                         #   token (e.g. "predicate_unresolved", "content_digest",
                                         #   "approver", "signature", "nonce", "mode", "profile")
        "surfaced": {                    # §8 MUST-surface, MUST-NOT-gate:
            "approver_id": str, "approver_act": str, "decision_date": str,
            "assurance_tier": str|None,
        },
        "references_unresolved": bool,   # free mode, §8.9 degrade (until MUST-resolve is chosen)
    }

`ledger` carries corpus context: `{"seen": [[approver_key_id, record_nonce], ...]}` for
the §4/§8.7 replay check, and (free mode) resolvable-reference hints for §8.9.
"""
import os
import sys


def verify(record, registry, ledger):  # pragma: no cover - no implementation yet
    raise NotImplementedError(
        "No cgr.cosign.v1 verifier exists yet. This bridge is the interface a future "
        "verifier implements; the corpus (vectors.json) is the target it must meet. "
        "See the module docstring for the verify() contract."
    )


if __name__ == "__main__":
    print(__doc__)
    sys.exit(0 if os.environ.get("CGR_COSIGN_VERIFIER") else 0)
