"""GRAFOMEM finding registry — loader, validator, emitter.

Single source of truth lives in findings.toml; the paper's Appendix C and the
results/W*-finding.md cards are PROJECTIONS emitted from here. A findings.lock
freezes the published identity of each number so the gate can prove the registry
only ever grows (append-only), never renumbers (immutable), never reuses a
retired number (durable identifiers).

This is the design reference for WS3's src/aml/eval/findings.py.
"""
from __future__ import annotations
import hashlib, json, sys, tomllib
from dataclasses import dataclass, asdict

ACTIVE = {"confirmed", "provisional", "needs-reconfirm"}   # occupy a slot
TOMBSTONE = "retired"                                       # slot kept, not reusable


@dataclass(frozen=True)
class Finding:
    id: str            # "F4"
    workload: str      # "W2"
    metric: str        # "M1"
    status: str        # confirmed | provisional | needs-reconfirm | retired
    embedder: str      # bge-small-en-v1.5 | stub | deterministic | invariant
    producer: str      # scripts/run_w2.py
    headline: str

    @property
    def n(self) -> int:
        return int(self.id[1:])

    def identity(self) -> str:
        """Hash of the immutable fields: what the number *means*. The headline
        value may be re-measured (stub->BGE) without changing identity; workload
        + metric + the number itself are the binding."""
        key = f"{self.id}|{self.workload}|{self.metric}"
        return hashlib.blake2b(key.encode(), digest_size=8).hexdigest()


def load(path: str) -> tuple[dict, list[Finding]]:
    raw = tomllib.load(open(path, "rb"))
    meta = {k: v for k, v in raw.items() if k != "finding"}
    fs = [Finding(**{k: f[k] for k in Finding.__dataclass_fields__}) for f in raw["finding"]]
    return meta, fs


def validate(fs: list[Finding], lock: dict | None = None) -> list[str]:
    """Returns a list of violation strings; empty == clean."""
    v: list[str] = []
    # 1. duplicate numbers — the F14/F15 collision class
    seen: dict[int, str] = {}
    for f in fs:
        if f.n in seen:
            v.append(f"duplicate number F{f.n}: '{seen[f.n]}' and '{f.workload}' both claim it")
        seen[f.n] = f.workload
    # 2. contiguity: 1..max must all be present (gaps only via tombstone)
    present = {f.n for f in fs}
    for i in range(1, max(present, default=0) + 1):
        if i not in present:
            v.append(f"gap at F{i}: registry must be contiguous (retire, don't delete)")
    # 3. append-only vs lock: published identities must not change or vanish
    if lock is not None:
        cur = {f.id: f.identity() for f in fs}
        for fid, ident in lock.items():
            if fid not in cur:
                v.append(f"{fid} removed — published numbers are durable; mark status=retired")
            elif cur[fid] != ident:
                v.append(f"{fid} identity changed — number rebound to a different finding (forbidden)")
    return v


def next_id(fs: list[Finding]) -> str:
    return f"F{max((f.n for f in fs), default=0) + 1}"


def make_lock(fs: list[Finding]) -> dict:
    return {f.id: f.identity() for f in fs}


def stale(fs: list[Finding], *, corpus_hash: str, embedder: str) -> list[Finding]:
    """Findings whose published numbers exist but whose evidence predates the
    current corpus/embedder — the freshness signal for the BGE pass + re-pins."""
    return [f for f in fs if f.status == "needs-reconfirm"
            or (f.embedder not in (embedder, "deterministic", "invariant"))]


# ── projections (what the paper / cards / report render) ────────────────────
def emit_appendix_c(fs: list[Finding], include: set[str]) -> str:
    rows = [f for f in fs if f.workload in include and f.status != TOMBSTONE]
    rows.sort(key=lambda f: f.n)
    out = ["| # | workload | finding (headline) |", "|---|---|---|"]
    out += [f"| {f.id} | {f.workload} | {f.headline} |" for f in rows]
    return "\n".join(out)


def emit_card(fs: list[Finding], workload: str) -> str:
    rows = sorted((f for f in fs if f.workload == workload), key=lambda f: f.n)
    body = "\n".join(f"- **{f.id}** ({f.metric}, {f.status}): {f.headline}" for f in rows)
    return f"# {workload} findings\n\n{body}\n"


if __name__ == "__main__":
    meta, fs = load("findings.toml")
    print(f"registry v{meta['registry_version']} · {meta['canonical_corpus']} · {len(fs)} findings\n")

    print("1) validate the seed:")
    viol = validate(fs)
    print("   " + ("CLEAN — next free id is " + next_id(fs) if not viol else "\n   ".join(viol)))

    print("\n2) inject the current bug (W10 mis-numbered F14/F15, as in the paper today):")
    broken = [f for f in fs if f.workload != "W10"] + [
        Finding("F14", "W10", "M8", "confirmed", "deterministic", "scripts/run_w10.py", "no_isolation over-claim"),
        Finding("F15", "W10", "M8", "confirmed", "deterministic", "scripts/run_w10.py", "resurrecting durability"),
    ]
    for msg in validate(broken):
        print("   VIOLATION:", msg)

    print("\n3) append-only gate — re-home a published number and re-validate:")
    lock = make_lock(fs)
    tampered = [Finding(f.id, "W99", f.metric, f.status, f.embedder, f.producer, f.headline)
                if f.id == "F4" else f for f in fs]
    for msg in validate(tampered, lock=lock):
        print("   VIOLATION:", msg)

    print("\n4) freshness (current = v1.0.0 / bge-small-en-v1.5):")
    s = stale(fs, corpus_hash=meta["corpus_hash"], embedder="bge-small-en-v1.5")
    print("   needs re-confirm:", ", ".join(f.id for f in s) or "none")

    print("\n5) paper Appendix C projection (W1-W6 + W10 — note the F14-F18 gap):\n")
    print(emit_appendix_c(fs, include={"W1", "W2", "W3", "W4", "W5", "W6", "W10"}))
