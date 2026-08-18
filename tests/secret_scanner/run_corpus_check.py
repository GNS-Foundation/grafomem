#!/usr/bin/env python3
"""Corpus verifier for the grafomem gitleaks rules (task d, Step 2).

Runs the ACTUAL shipped `.gitleaks.toml` against a generated corpus of positives and
negatives and asserts:

  * every positive (secret-bearing) line is flagged, and
  * zero negative lines are flagged.

The corpus is MATERIALIZED FRESH AT RUNTIME into a temp dir — nothing secret-shaped is
ever committed to the repo (so neither gitleaks nor GitGuardian trips on the fixtures,
and there is no committed key, dead or otherwise). Positives are the two grafomem traps
in every keyword + connector variant; negatives are real-codebase-shaped hashes, plain
base64, a git SHA, and adversarial "looks-secret-but-wrong-anchor" lines.

stdlib only (base64/os/secrets) so the minimal CI `secret-scan` job can run it without a
`pip install`. A Fernet key is just urlsafe-b64(32 random bytes); a Fernet token just
needs the `gAAAAA` prefix — neither requires the `cryptography` lib here.

Exit codes: 0 = corpus verified, 1 = corpus FAILED, 2 = gitleaks not installed.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
CONFIG = os.path.join(REPO, ".gitleaks.toml")


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()          # 44 chars, ends '='


def _fernet_token() -> str:
    return "gAAAAA" + base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")


def _build_corpus(dirpath: str) -> tuple[set[int], set[int]]:
    """Write positives.env + negatives.env; return their secret-line-number sets."""
    positives = [
        "# --- Trap 1: Fernet / provider-encryption key (3 keyword variants + connectors) ---",
        f"PROVIDER_ENCRYPTION_KEY={_fernet_key()}",
        f'os.environ["FERNET_SECRET"] = "{_fernet_key()}"',
        f'encryption_key: "{_fernet_key()}"',
        "# --- Trap 2: master key / KEK (3 keyword variants + connectors) ---",
        f"GRAFOMEM_MASTER_KEY={secrets.token_hex(32)}",
        f'MASTER_KEY = "{secrets.token_hex(32)}"',
        f"grafomem_kek: {secrets.token_hex(32)}",
        "# --- Trap 3: Fernet token (gAAAAA prefix) ---",
        f'CACHED_CIPHERTEXT = "{_fernet_token()}"',
    ]
    negatives = [
        "# legit values that MUST stay silent (no secret-key env-var anchor)",
        # real-codebase-shaped 64-hex hashes (payload_hash / schema_digest / breadcrumb_id)
        'payload_hash = "147c81d86e1814fef1b6fe8aeb0afeaf3e83fa3d66d04a6952526a815bda6b77"',
        'schema_digest = "1648377ee574a3bda2f177987dc8705dc9e7328e42e9002b13bab91b182a3a9b"',
        'breadcrumb_id = "17cb79fb2b4120f2b1ec65e4198d6e08b28e813feb01e4a400839b85e18080ce"',
        # ordinary base64 (decodes to readable text) and a 40-char git SHA
        'config_blob_b64 = "dGhlIHF1aWNrIGJyb3duIGZveCBqdW1wcyBvdg=="',
        'commit_sha = "15385815b1c1ffb78a4ae15d03f873861f07aea2"',
        # adversarial: "key" in the name + a Fernet-shaped value, but no fernet env-var
        # anchor → our fernet rule must stay silent (and it's clean under defaults too).
        f'public_key_fingerprint = "{_fernet_key()}"',
        # plain 64-hex content hash outside any master-key context — silent under our
        # master rule AND unambiguously not-a-secret to gitleaks' default rules.
        # (An `api_key_hash = <hex>` here would legitimately trip the DEFAULT
        # generic-api-key rule — reshaped rather than allow-listed, so the corpus stays
        # a clean "zero negatives under the full ruleset" guarantee.)
        f'content_sha256 = "{secrets.token_hex(32)}"',
    ]

    def _write(name: str, lines: list[str]) -> set[int]:
        with open(os.path.join(dirpath, name), "w") as f:
            f.write("\n".join(lines) + "\n")
        return {i for i, l in enumerate(lines, 1) if l.strip() and not l.startswith("#")}

    return _write("positives.env", positives), _write("negatives.env", negatives)


def main() -> int:
    if shutil.which("gitleaks") is None:
        print("SKIP/ERROR: gitleaks not installed — cannot verify the corpus.", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        pos_lines, neg_lines = _build_corpus(tmp)
        print(f"corpus: {len(pos_lines)} positive lines (must all flag), "
              f"{len(neg_lines)} negative lines (must all stay silent)")
        report = os.path.join(tmp, "report.json")
        proc = subprocess.run(
            ["gitleaks", "dir", tmp, "--config", CONFIG,
             "--report-format", "json", "--report-path", report,
             "--exit-code", "0", "--no-banner"],
            capture_output=True, text=True)
        if proc.returncode != 0:
            print("gitleaks invocation failed:\n" + proc.stderr, file=sys.stderr)
            return 1
        with open(report) as f:
            findings = json.load(f) or []

    flagged: dict[str, set[int]] = {"positives.env": set(), "negatives.env": set()}
    by_rule: dict[str, int] = {}
    for fnd in findings:
        base = os.path.basename(fnd.get("File", ""))
        if base in flagged:
            flagged[base].add(int(fnd.get("StartLine", 0)))
        if base == "positives.env":
            r = fnd.get("RuleID", "?")
            by_rule[r] = by_rule.get(r, 0) + 1

    caught, leaked_neg = flagged["positives.env"], flagged["negatives.env"]
    missed_pos = pos_lines - caught

    ok = True
    print(f"\npositives caught: {len(caught & pos_lines)}/{len(pos_lines)}")
    if missed_pos:
        ok = False
        print(f"  FAIL — positives NOT caught (lines): {sorted(missed_pos)}")
    print(f"negatives flagged: {len(leaked_neg)}/{len(neg_lines)} (must be 0)")
    if leaked_neg:
        ok = False
        print(f"  FAIL — negatives falsely flagged (lines): {sorted(leaked_neg)}")
    print("by-rule (positives):", by_rule)

    print("\nRESULT:", "PASS — all positives caught, zero negatives flagged" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
