"""
GRAFOMEM W7 — Conflict behavior classifier.

Turns a backend's answers to W7 conflict queries into the capability map the
spec calls for (01-workload-spec.md §4.7): one of six behavior classes. W7 is
the one workload that yields a class, not a scalar recall score — this module
is where that class is decided.

It is deliberately PURE: it operates on fact_ids (`bytes`) and small sets, with
no backend / oracle / trace imports, so it does not depend on any particular
backend interface. The W7 *runner* (next increment) adapts a backend's
retrieval output into the normalized inputs here:

  - the contested pair `(earlier_id, later_id)` for a conflict query, ordered by
    the facts' `sequence` — earlier = lower sequence = the "first" write, later =
    the "last" write (the W7 generator emits requires=[earlier, later], and the
    facts carry the authoritative sequence);
  - `returned_ids` — the fact_ids the backend's answer resolved to for THAT
    (subject, predicate) slot (the runner restricts the backend's reply to the
    contested slot before calling in);
  - `conflict_signaled` — whether the backend explicitly surfaced a conflict.
    This is the observable form of the CONFLICT_DETECTION capability; a backend
    without it can never reach CONFLICT_FLAG (spec §4.7). Wiring this channel is
    the runner's job and depends on the backend interface.

Per-query classification yields one of five deterministic outcomes;
NON_DETERMINISTIC is a cross-replay verdict (see `classify_backend`): a backend
that answers the SAME conflict differently across identical replays.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
from typing import Hashable


class BehaviorClass(Enum):
    """The six W7 behavior classes (spec §4.7). Values match the spec strings."""
    LAST_WRITE_WINS = "last_write_wins"
    FIRST_WRITE_WINS = "first_write_wins"
    MERGE = "merge"
    CONFLICT_FLAG = "conflict_flag"
    SILENT_DATA_LOSS = "silent_data_loss"
    NON_DETERMINISTIC = "non_deterministic"


def classify_query(
    earlier_id: bytes,
    later_id: bytes,
    returned_ids: Iterable[bytes],
    conflict_signaled: bool = False,
) -> BehaviorClass:
    """Classify a single conflict query's outcome.

    Precedence: an explicit conflict signal wins — surfacing the conflict is the
    defining CONFLICT_FLAG behavior regardless of what else came back. Otherwise
    the class is decided by which of the contested pair the answer contains:

        {later}            -> LAST_WRITE_WINS
        {earlier}          -> FIRST_WRITE_WINS
        {earlier, later}   -> MERGE
        neither present    -> SILENT_DATA_LOSS   (empty, or only other values)

    Never returns NON_DETERMINISTIC — that is a cross-replay verdict.
    """
    if earlier_id == later_id:
        raise ValueError("contested pair must be two distinct facts")
    if conflict_signaled:
        return BehaviorClass.CONFLICT_FLAG
    present = set(returned_ids) & {earlier_id, later_id}
    if present == {earlier_id, later_id}:
        return BehaviorClass.MERGE
    if present == {later_id}:
        return BehaviorClass.LAST_WRITE_WINS
    if present == {earlier_id}:
        return BehaviorClass.FIRST_WRITE_WINS
    return BehaviorClass.SILENT_DATA_LOSS


def classify_backend(
    replays: Sequence[Mapping[Hashable, BehaviorClass]],
) -> tuple[BehaviorClass, dict]:
    """Aggregate a backend's W7 finding across one or more replays.

    `replays` is a sequence of replays; each replay maps a stable per-query key
    (e.g. the query turn_id, comparable across replays of the SAME trace) to
    that query's `classify_query` outcome.

    Verdict:
      - NON_DETERMINISTIC if any key has DIFFERING outcomes across replays — the
        backend answered the same conflict two ways (true instability).
      - otherwise the dominant (modal) per-query class, the backend's stable
        strategy.

    The returned summary always carries the full distribution and the unstable
    count, so the capability map is visible even when a single class is named
    (e.g. a stable-but-mixed backend reads as its dominant class with a spread
    distribution; a uniform backend reads as a single spike).
    """
    if not replays:
        raise ValueError("need at least one replay")

    outcomes_per_key: dict[Hashable, set[BehaviorClass]] = {}
    for replay in replays:
        for key, outcome in replay.items():
            outcomes_per_key.setdefault(key, set()).add(outcome)

    unstable = {k for k, outs in outcomes_per_key.items() if len(outs) > 1}

    dist: Counter[BehaviorClass] = Counter()
    for outs in outcomes_per_key.values():
        if len(outs) == 1:
            dist[next(iter(outs))] += 1

    summary = {
        "distribution": {bc.value: n for bc, n in dist.items()},
        "n_queries": len(outcomes_per_key),
        "n_unstable": len(unstable),
    }

    if unstable or not dist:
        return BehaviorClass.NON_DETERMINISTIC, summary
    dominant, _ = dist.most_common(1)[0]
    return dominant, summary


# ============================================================================
# Smoke check — run `python w7_classify.py`
# ============================================================================

if __name__ == "__main__":
    print("GRAFOMEM w7_classify.py — conflict behavior classifier\n")

    A = b"\x01" * 16   # earlier write (first)
    B = b"\x02" * 16   # later write   (last)
    X = b"\x09" * 16   # an unrelated / spurious value

    # --- Test 1: the five deterministic per-query outcomes ----------------
    assert classify_query(A, B, {B}) is BehaviorClass.LAST_WRITE_WINS
    assert classify_query(A, B, {A}) is BehaviorClass.FIRST_WRITE_WINS
    assert classify_query(A, B, {A, B}) is BehaviorClass.MERGE
    assert classify_query(A, B, set()) is BehaviorClass.SILENT_DATA_LOSS
    assert classify_query(A, B, {X}) is BehaviorClass.SILENT_DATA_LOSS  # only spurious
    print("✓ Per-query outcomes                 (lww / fww / merge / silent_data_loss)")

    # --- Test 2: conflict_flag + its precedence ---------------------------
    assert classify_query(A, B, set(), conflict_signaled=True) is BehaviorClass.CONFLICT_FLAG
    assert classify_query(A, B, {B}, conflict_signaled=True) is BehaviorClass.CONFLICT_FLAG
    assert classify_query(A, B, {A, B}, conflict_signaled=True) is BehaviorClass.CONFLICT_FLAG
    print("✓ conflict_flag + precedence         (signal wins over any returned set)")

    # --- Test 3: spurious extras don't change which contested value won ---
    assert classify_query(A, B, {B, X}) is BehaviorClass.LAST_WRITE_WINS
    assert classify_query(A, B, {A, B, X}) is BehaviorClass.MERGE
    print("✓ Robust to spurious extras          (classifies on the contested pair only)")

    # --- Test 4: guards ----------------------------------------------------
    try:
        classify_query(A, A, {A}); raise AssertionError("expected ValueError")
    except ValueError:
        pass
    try:
        classify_backend([]); raise AssertionError("expected ValueError")
    except ValueError:
        pass
    print("✓ Guards                             (identical pair / no replays rejected)")

    # --- Test 5: backend aggregation — uniform strategy -------------------
    lww_replay = {f"q{i}": BehaviorClass.LAST_WRITE_WINS for i in range(10)}
    cls, summ = classify_backend([lww_replay])
    assert cls is BehaviorClass.LAST_WRITE_WINS
    assert summ["n_unstable"] == 0 and summ["distribution"] == {"last_write_wins": 10}
    print(f"✓ Backend: uniform                   ({cls.value}, dist={summ['distribution']})")

    # --- Test 6: backend aggregation — true non-determinism ---------------
    # Same query key, different outcome across two replays of the SAME trace.
    r1 = {"q0": BehaviorClass.LAST_WRITE_WINS, "q1": BehaviorClass.MERGE}
    r2 = {"q0": BehaviorClass.FIRST_WRITE_WINS, "q1": BehaviorClass.MERGE}
    cls, summ = classify_backend([r1, r2])
    assert cls is BehaviorClass.NON_DETERMINISTIC, cls
    assert summ["n_unstable"] == 1  # q0 flipped; q1 stable
    print(f"✓ Backend: non_deterministic         (q0 flips across replays; n_unstable={summ['n_unstable']})")

    # --- Test 7: backend aggregation — stable-but-mixed reads as dominant -
    mixed = {f"q{i}": (BehaviorClass.LAST_WRITE_WINS if i < 7 else BehaviorClass.FIRST_WRITE_WINS)
             for i in range(10)}
    cls, summ = classify_backend([mixed])
    assert cls is BehaviorClass.LAST_WRITE_WINS              # dominant
    assert summ["n_unstable"] == 0
    assert summ["distribution"] == {"last_write_wins": 7, "first_write_wins": 3}
    print(f"✓ Backend: stable-but-mixed          (dominant {cls.value}, spread {summ['distribution']})")

    # --- Test 8: a CONFLICT_DETECTION backend reads as conflict_flag ------
    flag_replay = {f"q{i}": BehaviorClass.CONFLICT_FLAG for i in range(10)}
    cls, _ = classify_backend([flag_replay])
    assert cls is BehaviorClass.CONFLICT_FLAG
    print(f"✓ Backend: conflict-aware            ({cls.value} — the CONFLICT_DETECTION outcome)")

    # --- Test 9: enum values match the spec strings -----------------------
    assert {b.value for b in BehaviorClass} == {
        "last_write_wins", "first_write_wins", "merge",
        "conflict_flag", "silent_data_loss", "non_deterministic",
    }
    print("✓ Six classes, spec-string values    (matches §4.7)")

    print("\nAll classifier smoke checks green. Pure decision logic locked; "
          "run_w7 (real backends -> these inputs) is next.")
