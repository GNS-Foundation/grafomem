"""cgr.disposition.v1 conformance — the executable target for the runtime record class + routes.

Two layers (mirrors tests/test_cosign_conformance.py):
1. `test_corpus_wellformed` — ALWAYS runs. Verifies the corpus is internally consistent against the
   reference verifier (packages/grafomem-cgr cosign_verify): every `expect_verify` verdict holds
   under the fixture registry + the pinned trusted issuer. Green before any runtime route exists.
2. `test_runtime_conformance` — the RUNTIME leg. Skipped unless CGR_DISPOSITION_BASE + a
   disposition:write key are set. POSTs each vector to /v1/dispositions and asserts `http_expect`.
   This FAILS against the current runtime (the route does not exist yet) — it is the corpus-first
   target the implementation must turn green. See conformance/cgr-disposition-v1/.

Profile registry here is the corpus FIXTURE (normative entry unwritten, spec §11 Q3 — a Foundation
ratification per ADR-0008).
"""
import importlib.util
import json
import os
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CORPUS = _ROOT / "conformance" / "cgr-disposition-v1"

# Load the reference cosign verifier by path (no packaging assumption).
_spec = importlib.util.spec_from_file_location(
    "cosign_verify", _ROOT / "packages" / "grafomem-cgr" / "src" / "grafomem_cgr" / "cosign_verify.py")
cosign_verify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cosign_verify)


def _load(name):
    return json.loads((_CORPUS / name).read_text())


def test_corpus_wellformed():
    corpus = _load("vectors.json")
    registry = _load("registry.json")
    pinned = corpus["keys"]["pinned_issuer_pub"]  # the SOLE trusted issuer (runtime pinned key)
    assert corpus["vectors"], "empty corpus"
    for v in corpus["vectors"]:
        res = cosign_verify.verify(v["record"], registry, trusted_issuers={pinned})
        want_valid = v["expect_verify"] == "valid"
        assert res.get("valid") is want_valid, (
            f"{v['id']} ({v['title']}): expected verify valid={want_valid}, got {res}")


def test_corpus_pins_the_registry_gap():
    corpus = _load("vectors.json")
    # Guard that the corpus stays honest about the normative gap until the registry is ratified.
    assert "FIXTURE" in corpus["profile_registry_status"]
    assert "§11 Q3" in corpus["profile_registry_status"] or "11 Q3" in corpus["profile_registry_status"]


@pytest.mark.skipif(not os.environ.get("CGR_DISPOSITION_BASE"),
                    reason="runtime leg: set CGR_DISPOSITION_BASE + CGR_DISPOSITION_KEY to run against a deployed runtime")
def test_runtime_conformance():
    import subprocess
    base = os.environ["CGR_DISPOSITION_BASE"].rstrip("/")
    key = os.environ["CGR_DISPOSITION_KEY"]
    corpus = _load("vectors.json")
    failures = []
    for v in corpus["vectors"]:
        if v["http_expect"] is None:   # verify-only vector (e.g. wrong system key)
            continue
        # POST body = the approver-signed inner (runtime counter-signs); strip the system layer.
        body = {k: val for k, val in v["record"].items()
                if k not in ("system_signature", "system_metadata")}
        cmd = ["curl", "-s", "-A", "Mozilla/5.0 (grafomem-ops)", "-X", "POST",
               f"{base}/v1/dispositions", "-w", "\n__S__%{http_code}",
               "-H", f"X-API-Key: {key}", "-H", "Content-Type: application/json",
               "-d", json.dumps(body)]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=40).stdout
        _, _, st = out.rpartition("__S__")
        if int(st or 0) != v["http_expect"]:
            failures.append(f"{v['id']} ({v['title']}): expected HTTP {v['http_expect']}, got {st}")
    assert not failures, "runtime conformance failures:\n" + "\n".join(failures)
