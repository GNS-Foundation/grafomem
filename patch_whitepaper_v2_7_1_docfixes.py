#!/usr/bin/env python3
"""Surgical doc fixes for grafomem-cloud-whitepaper-v2_7_1.md (v2.7.1 hygiene pass).

Edits:
  1. §24  TRIP scope: "Point-in-time" -> longitudinal proof-of-humanity/anti-Sybil framing
  2. §24.2 TRIP hash-chain cell: BLAKE2b -> SHA-256 (TRIP guardrail: SHA-256/Ed25519)
  3. Footer: Version 2.7.0 -> 2.7.1, relocated from mid-document (after §28.7) to true end

Idempotent: re-running is a no-op. Asserts guard every match.
Usage: python3 patch_whitepaper_v2_7_1_docfixes.py [path-to-whitepaper]
"""
import re
import subprocess
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else "grafomem-cloud-whitepaper-v2_7_1.md")
assert path.exists(), f"File not found: {path}"
text = path.read_text(encoding="utf-8")
orig = text

# ---------- Edit 1: §24 TRIP scope cell ----------
OLD_SCOPE = "Point-in-time identity attestation via spatially quantized breadcrumbs"
NEW_SCOPE = "Longitudinal proof-of-humanity / anti-Sybil attestation via spatially quantized breadcrumbs"
if OLD_SCOPE in text:
    assert text.count(OLD_SCOPE) == 1, "Edit 1: expected exactly one occurrence"
    text = text.replace(OLD_SCOPE, NEW_SCOPE)
    print("[1] §24 TRIP scope -> longitudinal framing: APPLIED")
else:
    assert NEW_SCOPE in text, "Edit 1: neither old nor new text found — file diverged"
    print("[1] §24 TRIP scope: already applied")

# ---------- Edit 2: §24.2 TRIP hash-chain cell ----------
OLD_ROW = "| Hash chain | BLAKE2b (epochs) | BLAKE2b-256 | SHA-256 |"
NEW_ROW = "| Hash chain | SHA-256 (epochs) | BLAKE2b-256 | SHA-256 |"
if OLD_ROW in text:
    assert text.count(OLD_ROW) == 1, "Edit 2: expected exactly one occurrence"
    text = text.replace(OLD_ROW, NEW_ROW)
    print("[2] §24.2 TRIP hash row BLAKE2b -> SHA-256: APPLIED")
else:
    assert NEW_ROW in text, "Edit 2: neither old nor new row found — file diverged"
    print("[2] §24.2 TRIP hash row: already applied")

# ---------- Edit 3: footer version bump + relocation to true end ----------
footer_re = re.compile(r"\n---\n\n(\*End of document\. Version 2\.7\.\d[^\n]*\*)\n")
m = footer_re.search(text)
already_at_end = text.rstrip().endswith("*") and "*End of document. Version 2.7.1" in text.rstrip()[-2500:]

if m and not (already_at_end and m.start() > len(text) - 3000):
    footer = m.group(1).replace("Version 2.7.0", "Version 2.7.1")
    assert "Version 2.7.1" in footer, "Edit 3: version bump failed"
    # Remove from mid-document position (keep a single --- separator between §28.7 and §29)
    text = footer_re.sub("\n", text, count=1)
    text = text.replace("\n\n\n---", "\n\n---")  # normalize gap left at old position
    # Append at the true end of the document
    text = text.rstrip() + "\n\n---\n\n" + footer + "\n"
    print("[3] Footer: bumped to 2.7.1 and relocated to end of document: APPLIED")
else:
    assert "*End of document. Version 2.7.1" in text, "Edit 3: footer not found in expected state"
    print("[3] Footer: already applied")

# ---------- Write + verify ----------
if text != orig:
    path.write_text(text, encoding="utf-8")
    print(f"\nWrote {path} ({len(text.splitlines())} lines)")
else:
    print("\nNo changes needed — file already patched.")

# Post-conditions
assert "Point-in-time identity attestation" not in text
assert text.count("Longitudinal proof-of-humanity / anti-Sybil attestation") == 1
assert OLD_ROW not in text and NEW_ROW in text
assert "Version 2.7.0 — 26 sprints" not in text
assert text.rstrip().endswith("SLO alerting Defined-not-Active.*"), "Footer is not at document end"
print("Post-condition asserts: ALL PASS")

# ---------- Git (only if the file lives inside a work tree) ----------
try:
    inside = subprocess.run(
        ["git", "-C", str(path.parent), "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True,
    )
    if inside.returncode == 0 and inside.stdout.strip() == "true" and text != orig:
        subprocess.run(["git", "-C", str(path.parent), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(path.parent), "commit", "-m",
             "docs(whitepaper): v2.7.1 hygiene — TRIP longitudinal framing + SHA-256, footer bumped/relocated"],
            check=True,
        )
        push = subprocess.run(["git", "-C", str(path.parent), "push"], capture_output=True, text=True)
        if push.returncode == 0:
            print("Git: committed and pushed.")
        else:
            print(f"Git: committed; push failed (do it manually): {push.stderr.strip().splitlines()[-1] if push.stderr else 'unknown'}")
    else:
        print("Git: skipped (not a work tree, or no changes).")
except FileNotFoundError:
    print("Git: not available — skipped.")
