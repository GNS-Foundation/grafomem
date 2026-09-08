#!/usr/bin/env python3
"""Bridge: drive the reference `cgr.cosign.v1` verifier (clients/cgr-verify, JS) from the
Python runner. Set `CGR_COSIGN_VERIFIER=conformance/cgr-cosign-v1/verify_bridge.py`.

Shells to clients/cgr-verify/bin/verify-cosign.mjs (needs `node`), passing
{record, registry, ledger, trusted_issuers} on stdin and reading the VerifyResult JSON on
stdout — the same shape as the v4 bridge. The verifier implements docs/cgr/cgr-cosign-v1-spec.md
§8 (amended by decisions 0010 and 0011); this corpus is the target it must meet.

`trusted_issuers` is the REQUIRED trusted issuer set (decision 0011 §8.2a) — no default. The
runner derives it from each vector's `pinned_issuer`; the JS CLI consumes it once the verifiers
PR wires step 2a through.
"""
import json
import pathlib
import subprocess

_MJS = pathlib.Path(__file__).resolve().parents[2] / "clients" / "cgr-verify" / "bin" / "verify-cosign.mjs"


def verify(record, registry, ledger, trusted_issuers):
    proc = subprocess.run(
        ["node", str(_MJS)],
        input=json.dumps({"record": record, "registry": registry, "ledger": ledger,
                          "trusted_issuers": trusted_issuers}),
        capture_output=True, text=True,
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"verify-cosign.mjs failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


if __name__ == "__main__":
    print(f"bridge -> {_MJS}")
