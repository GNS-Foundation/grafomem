"""CGR_V4_VERIFIER bridge: drive the reference verifier (clients/cgr-verify, JS) from the
Python conformance runner. Shells to bin/verify-v4.mjs (one node process per vector).

    CGR_V4_VERIFIER=conformance/cgr-attestation-v4/verify_bridge.py \
        pytest tests/test_v4_conformance.py -v
"""
import json, subprocess, pathlib

_HERE = pathlib.Path(__file__).resolve().parent
_CLI = _HERE.parent.parent / "clients" / "cgr-verify" / "bin" / "verify-v4.mjs"


def verify(subject, ledger, pinned_issuer_hex, held_edges=(), mode=None, seek_fails=False):
    payload = json.dumps({
        "subject": subject, "ledger": ledger,
        "pinned_issuer": pinned_issuer_hex, "held_edges": list(held_edges or []),
        "mode": mode, "seek_fails": bool(seek_fails),
    })
    out = subprocess.run(
        ["node", str(_CLI)], input=payload, capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0 and not out.stdout.strip():
        raise RuntimeError(f"node verifier failed: {out.stderr.strip()}")
    return json.loads(out.stdout)
