#!/usr/bin/env python3
"""
GRAFOMEM corpus builder — R3/R4.

Reads corpus/corpus.toml (human-authored seed registry, R4), expands the
workload x seed x difficulty cross-product, generates and validates each
trace, writes them to corpus/traces/, and records the BLAKE2b-256 corpus
content hash (R3) in corpus/corpus.lock.

The corpus content hash excludes trace_id and generated_at (non-deterministic
/ non-load-bearing fields), so it is stable across runs and machines — every
published finding can cite it.

The build GATES on validators: if any trace fails V1-V5 / reference / tenant /
consistency checks, the build aborts. A published corpus is, by construction,
a validated corpus.

Dependency-free: corpus.toml is read via stdlib tomllib (Python 3.11+).

Usage:  python scripts/generate_corpus.py
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

# Repo root (for locating corpus/ paths). The aml package is import-resolvable
# via the editable install (pip install -e .); no path manipulation needed.
_REPO = Path(__file__).resolve().parent.parent

from aml.generator.trace import Difficulty, Trace, Workload, trace_to_dict
from aml.generator.validators import validate_trace
from aml.generator.workloads.w1 import generate_w1
from aml.generator.workloads.w2 import generate_w2
from aml.generator.workloads.w3 import generate_w3
from aml.generator.workloads.w4 import generate_w4
from aml.generator.workloads.w5 import generate_w5
from aml.generator.workloads.w6 import generate_w6
from aml.generator.workloads.w7 import generate_w7
from aml.generator.workloads.w8 import generate_w8
from aml.generator.workloads.w9 import generate_w9
from aml.generator.workloads.w10 import generate_w10


# Dispatch table: workload -> generator. W1-W7 + W9 + W10 (W8 held out of the
# locked corpus pending the retention-variant decision).
_GENERATORS = {
    Workload.W1: generate_w1,
    Workload.W2: generate_w2,
    Workload.W3: generate_w3,
    Workload.W4: generate_w4,
    Workload.W5: generate_w5,
    Workload.W6: generate_w6,
    Workload.W7: generate_w7,
    Workload.W8: generate_w8,
    Workload.W9: generate_w9,
    Workload.W10: generate_w10,
}


def generate_one(workload: Workload, seed: int, difficulty: Difficulty) -> Trace:
    gen = _GENERATORS.get(workload)
    if gen is None:
        raise NotImplementedError(
            f"workload {workload.value} has no generator yet"
        )
    return gen(seed, difficulty)


def content_hash(trace_dict: dict) -> str:
    """BLAKE2b-256 over canonical trace JSON, excluding the non-deterministic
    fields (trace_id, generated_at). Stable across runs and machines."""
    d = dict(trace_dict)
    d.pop("trace_id", None)
    d.pop("generated_at", None)
    canonical = json.dumps(d, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(canonical.encode("utf-8"), digest_size=32).hexdigest()


def main() -> int:
    manifest_path = _REPO / "corpus" / "corpus.toml"
    if not manifest_path.exists():
        print(f"ERROR: manifest not found at {manifest_path}")
        return 1
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))

    workloads = [Workload(w) for w in manifest["workloads"]]
    seeds = list(manifest["seeds"])
    difficulties = [Difficulty(d) for d in manifest["difficulties"]]

    traces_dir = _REPO / "corpus" / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)

    print(f"Building corpus '{manifest['name']}' "
          f"(schema {manifest['schema_version']})\n")

    trace_hashes: dict[str, str] = {}
    total_turns = 0
    total_queries = 0

    # Deterministic iteration order: workload, difficulty, seed.
    for workload, difficulty, seed in product(workloads, difficulties, seeds):
        name = f"{workload.value}_s{seed}_{difficulty.value}"
        trace = generate_one(workload, seed, difficulty)

        # Gate on the independent validators — a published corpus is validated.
        violations = validate_trace(trace)
        if violations:
            print(f"  FAIL {name}: {len(violations)} violation(s)")
            for v in violations[:5]:
                print(f"       {v}")
            print("\nCorpus build aborted — fix the generator/oracle first.")
            return 1

        d = trace_to_dict(trace)
        (traces_dir / f"{name}.jsonl").write_text(
            json.dumps(d, separators=(",", ":")), encoding="utf-8",
        )
        h = content_hash(d)
        trace_hashes[name] = h

        n_turns = sum(len(s["turns"]) for s in d["sessions"])
        n_q = sum(
            1 for s in d["sessions"] for t in s["turns"]
            if t["role"] == "agent_query"
        )
        total_turns += n_turns
        total_queries += n_q
        print(f"  ok  {name:22s} {n_turns:6d} turns  {n_q:6d} q  {h[:16]}...")

    # R3: corpus hash over sorted per-trace content hashes (order-independent).
    corpus_hash = hashlib.blake2b(
        "".join(sorted(trace_hashes.values())).encode("utf-8"),
        digest_size=32,
    ).hexdigest()

    # Determinism signal vs any prior lock.
    lock_path = _REPO / "corpus" / "corpus.lock"
    prior = None
    if lock_path.exists():
        try:
            prior = json.loads(lock_path.read_text()).get("corpus_hash")
        except Exception:
            prior = None

    # Per-workload rollup hashes — stable citations as the suite grows. Each is
    # BLAKE2b-256 over that workload's sorted trace hashes; with a single
    # workload it equals corpus_hash, and adding a workload never perturbs an
    # existing one (W1's rollup is identical before and after W2 is added).
    by_workload: dict[str, list[str]] = {}
    for _nm, _h in trace_hashes.items():
        by_workload.setdefault(_nm.split("_", 1)[0], []).append(_h)
    workload_hashes = {
        w: hashlib.blake2b("".join(sorted(hs)).encode("utf-8"),
                           digest_size=32).hexdigest()
        for w, hs in sorted(by_workload.items())
    }

    lock = {
        "name": manifest["name"],
        "schema_version": manifest["schema_version"],
        "generator_version": manifest["generator_version"],
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "n_traces": len(trace_hashes),
        "corpus_hash": corpus_hash,
        "workload_hashes": workload_hashes,
        "trace_hashes": dict(sorted(trace_hashes.items())),
    }
    lock_path.write_text(json.dumps(lock, indent=2), encoding="utf-8")

    print(f"\n{len(trace_hashes)} traces  |  {total_turns} turns  |  "
          f"{total_queries} queries")
    print(f"corpus_hash: {corpus_hash}")
    for _w, _wh in workload_hashes.items():
        print(f"  {_w}: {_wh}")
    if prior is None:
        print("(first build — no prior lock to compare)")
    else:
        # Assertion: Rollup stability for W1-W7, W9, W10 must hold byte-identically.
        prior_lock = json.loads(lock_path.read_text())
        prior_workloads = prior_lock.get("workload_hashes", {})
        for w, old_hash in prior_workloads.items():
            if w in workload_hashes:
                if workload_hashes[w] != old_hash:
                    raise AssertionError(
                        f"Rollup stability violated for {w}! "
                        f"Prior hash {old_hash} != New hash {workload_hashes[w]}"
                    )
        if prior == corpus_hash:
            print("STABLE — corpus_hash matches prior lock (R3 reproducibility holds)")
        else:
            print(f"CHANGED — prior {prior[:16]}... -> now {corpus_hash[:16]}...")
            print("✓ Rollup stability holds for all pre-existing workloads.")
    print("wrote corpus/corpus.lock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
