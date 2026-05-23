"""
W10 experiment: concurrency & isolation — the integrity axis under contention.

When concurrent transactions touch the same memory, a store's *declared* isolation
level is a claim, not a guarantee. W10 plants four probes per group and the outcome
oracle (aml.eval.concurrency) reads the achieved behavior off the result:

  - lost update        two writers, same key. A store that loses one writer's
                       effect is read_committed; one that chains or first-committer-
                       aborts is snapshot-or-stronger.
  - write skew         two writers, disjoint keys, each reading the other's. Only a
                       serializable (SSI) store aborts one; snapshot admits it. This
                       is the probe that separates serializable from snapshot.
  - non-repeatable     a reader's two reads bracket a writer. Differing reads are
                       read_committed; repeatable reads are snapshot-or-stronger.
  - resurrection       a committed delete vs a concurrent supersede (§10.4). Reviving
                       the deleted key is a durability violation at every level.

Five backends span the achieved-level lattice and the claim<->behavior gap:
  - serializable   declares + achieves serializable.
  - snapshot       declares + achieves snapshot (looks serializable on lost-update
                   and non-repeatable; the write-skew probe unmasks it).
  - read_committed declares + achieves read_committed.
  - no_isolation   behaves at read_committed but CLAIMS serializable — the over-
                   claimer (the concurrency analog of leaky_tenant). Downgraded.
  - resurrecting   serializable on the anomaly probes, but resurrects a committed
                   delete — a §10.4 violation.

A declared level does not certify isolation: no_isolation accepts a serializable
policy and runs every transaction, yet delivers read_committed. Only the probes,
scored against the lattice, separate the five. Synthetic stores — no embedder,
no real threads (§10.6); each realizes one admissible execution per probe.
"""

from __future__ import annotations

from statistics import mean

from aml.backends.isolation_backends import (
    NoIsolationStore,
    ReadCommittedStore,
    ResurrectingStore,
    SerializableStore,
    SnapshotStore,
)
from aml.eval.concurrency_runner import run_w10, summarize
from aml.generator.trace import Difficulty, TxnAnomaly
from aml.generator.workloads.w10 import generate_w10

SEEDS = range(5)
DIFF = Difficulty.HARD

_WORST = {"OK": 0, "DOWNGRADE": 1, "VIOLATION": 2}

BACKENDS = [
    ("serializable",   SerializableStore),
    ("snapshot",       SnapshotStore),
    ("read_committed", ReadCommittedStore),
    ("no_isolation",   NoIsolationStore),
    ("resurrecting",   ResurrectingStore),
]


def m8_conformance(verdicts) -> float:
    """M8 (proposed) — isolation conformance: fraction of a trace's probes on
    which the store HONORS its declared policy (no over-claim downgrade, no §10.4
    resurrection). 1.0 = behavior matches claim on every probe; < 1.0 = the store
    over-claims or violates durability somewhere. The achieved LEVEL (below) is the
    categorical headline; M8 is the scalar roll-up for cross-backend comparison."""
    if not verdicts:
        return float("nan")
    return sum(1 for v in verdicts if v.status == "OK") / len(verdicts)


def measure(seeds=SEEDS, difficulty=DIFF):
    rows: dict[str, dict] = {}
    for name, cls in BACKENDS:
        achieved: set = set()
        durable = True
        m8s: list[float] = []
        overalls: list[str] = []
        declared = None
        for s in seeds:
            tr = generate_w10(seed=s, difficulty=difficulty)
            store = cls()
            declared = store.declared_policy.level
            verdicts = run_w10(store, tr)
            summ = summarize(verdicts)
            achieved.add(summ["combined_achieved"])
            overalls.append(summ["overall"])
            m8s.append(m8_conformance(verdicts))
            if any(v.status == "VIOLATION" for v in verdicts
                   if v.anomaly is TxnAnomaly.RESURRECTION):
                durable = False
        rows[name] = {
            "declared": declared,
            "achieved": achieved,                       # singleton if deterministic
            "durable": durable,
            "m8": mean(m8s),
            "overall": max(overalls, key=lambda x: _WORST[x]),
        }
    return rows


def main():
    n = len(list(SEEDS))
    rows = measure()
    print(f"\nW10 concurrency & isolation ({DIFF.value}, {n} seeds):\n")
    print(f"  {'backend':16s} {'declared':15s} {'achieved':15s} {'durable':8s} "
          f"{'M8':>5s}   verdict")
    print("  " + "-" * 78)
    for name, _ in BACKENDS:
        r = rows[name]
        ach = "/".join(sorted(a or "-" for a in r["achieved"]))  # flags non-determinism
        print(f"  {name:16s} {r['declared'].value:15s} {ach:15s} "
              f"{('yes' if r['durable'] else 'NO'):8s} {r['m8']:5.2f}   {r['overall']}")
    print("  " + "-" * 78)
    print("\n  'achieved' is what the store DELIVERS; 'declared' is what it CLAIMS.")
    print("  no_isolation claims serializable but delivers read_committed (over-claimer);")
    print("  resurrecting holds serializable on the anomaly probes but revives a committed")
    print("  delete (§10.4, durable=NO). Claim != behavior; only the probes separate them.")
    print("\n  M8 (proposed) = fraction of probes the store's behavior honors its claim.")


if __name__ == "__main__":
    main()
