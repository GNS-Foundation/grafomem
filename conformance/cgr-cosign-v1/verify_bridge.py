#!/usr/bin/env python3
"""Bridge: drive the reference `cgr.cosign.v1` verifier (clients/cgr-verify, JS) from the
Python runner. Set `CGR_COSIGN_VERIFIER=conformance/cgr-cosign-v1/verify_bridge.py`.

Shells to clients/cgr-verify/bin/verify-cosign.mjs (needs `node`), passing
{record, registry, ledger} on stdin and reading the VerifyResult JSON on stdout —
the same shape as the v4 bridge. The verifier implements docs/cgr/cgr-cosign-v1-spec.md
§8 (amended by decision 0010); this corpus is the target it must meet.
"""
import json
import pathlib
import subprocess

_MJS = pathlib.Path(__file__).resolve().parents[2] / "clients" / "cgr-verify" / "bin" / "verify-cosign.mjs"


def verify(record, registry, ledger):
    proc = subprocess.run(
        ["node", str(_MJS)],
        input=json.dumps({"record": record, "registry": registry, "ledger": ledger}),
        capture_output=True, text=True,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"verify-cosign.mjs failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


if __name__ == "__main__":
    print(f"bridge -> {_MJS}")
