"""
GRAFOMEM trace validators — v0.1.0.

Independent re-checks of the V-rules in 01-workload-spec.md §7.3, plus
reference integrity, tenant isolation, and ground-truth consistency.

Design discipline: this module does NOT trust the oracle. It re-derives the
deletion ledger and introduction times from the raw turn stream itself, then
checks the trace's GroundTruth against that independent derivation. If the
oracle ever drifts from the spec, the validator catches the disagreement.
This is what a corpus-builder gates on before accepting a trace.

Rules checked:
    V1  intra-turn disjointness (introduces ∩ deletes = ∅)
    V2  live-target deletion (delete only existing, not-already-deleted facts)
    V3  no dangling chain references in final facts; chains terminate
    V4  required facts are retrievable (requires ⊆ active_memory)
    V5  deletion ledger is reproducible from the turn stream
    REF reference integrity (every fact_id resolves to known universe)
    TENANT  introduces don't cross tenant boundaries (W5)
    CONSISTENCY  recall_targets == requires
    ACTIVE  active_memory soundness (no deleted/unintroduced/wrong-tenant facts)

Returns ALL violations found (does not stop at the first), so a corpus
report can list everything wrong with a bad trace at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

try:
    from trace import Trace, TurnRole  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    from aml.generator.trace import Trace, TurnRole


@dataclass(slots=True)
class Violation:
    rule: str
    message: str
    location: str | None = None

    def __str__(self) -> str:
        loc = f" [{self.location}]" if self.location else ""
        return f"{self.rule}: {self.message}{loc}"


class ValidationFailed(Exception):
    """Raised by validate_trace_strict on the first violation."""
    def __init__(self, violation: Violation):
        self.violation = violation
        super().__init__(str(violation))


def validate_trace(trace: Trace) -> list[Violation]:
    """Run all checks. Returns a list of every violation found (empty = valid)."""
    v: list[Violation] = []
    gt = trace.ground_truth

    final_ids = {f.fact_id for f in trace.facts}
    final_index = {f.fact_id: f for f in trace.facts}
    deleted_ids = set(gt.deleted_facts)
    known = final_ids | deleted_ids  # universe a reference may resolve to

    # --- Total turn order (timestamp, session_index, turn_index) ----------
    ordered = []
    for si, s in enumerate(trace.sessions):
        for ti, turn in enumerate(s.turns):
            ordered.append((turn.timestamp, si, ti, turn, s.tenant_id))
    ordered.sort(key=lambda x: (x[0], x[1], x[2]))

    # --- Forward pass: V1, REF, V2, re-derive ledger + introduction times -
    introduced_at: dict[bytes, datetime] = {}
    re_deleted: dict[bytes, datetime] = {}

    for ts, _si, _ti, turn, _tenant in ordered:
        loc = str(turn.turn_id)

        # V1
        overlap = set(turn.introduces) & set(turn.deletes)
        if overlap:
            v.append(Violation(
                "V1",
                f"turn introduces and deletes the same fact(s) "
                f"{[b.hex()[:12] for b in sorted(overlap)]}",
                loc,
            ))

        # REF — every referenced fact_id must resolve to the known universe
        for fid in (*turn.introduces, *turn.deletes, *turn.requires):
            if fid not in known:
                v.append(Violation(
                    "REF",
                    f"references unknown fact {fid.hex()[:12]} "
                    f"(not in final facts or deletion ledger)",
                    loc,
                ))

        # introduce
        for fid in turn.introduces:
            introduced_at.setdefault(fid, ts)

        # delete — V2
        for fid in turn.deletes:
            if fid not in introduced_at:
                v.append(Violation(
                    "V2",
                    f"deletes fact {fid.hex()[:12]} never introduced",
                    loc,
                ))
            elif fid in re_deleted:
                v.append(Violation(
                    "V2",
                    f"deletes already-deleted fact {fid.hex()[:12]}",
                    loc,
                ))
            else:
                re_deleted[fid] = ts

    # --- V5 — re-derived deletion ledger must match GroundTruth -----------
    if re_deleted != dict(gt.deleted_facts):
        extra = set(gt.deleted_facts) - set(re_deleted)
        missing = set(re_deleted) - set(gt.deleted_facts)
        wrong_time = {
            fid for fid in set(re_deleted) & set(gt.deleted_facts)
            if re_deleted[fid] != gt.deleted_facts[fid]
        }
        if extra:
            v.append(Violation(
                "V5",
                f"deletion ledger has {len(extra)} fact(s) with no "
                f"corresponding delete turn "
                f"(e.g. {next(iter(extra)).hex()[:12]})",
            ))
        if missing:
            v.append(Violation(
                "V5",
                f"deletion ledger missing {len(missing)} fact(s) that were "
                f"deleted by turns (e.g. {next(iter(missing)).hex()[:12]})",
            ))
        if wrong_time:
            v.append(Violation(
                "V5",
                f"deletion ledger has wrong timestamp for {len(wrong_time)} "
                f"fact(s) (e.g. {next(iter(wrong_time)).hex()[:12]})",
            ))

    # --- Per-query checks: CONSISTENCY, V4, ACTIVE, TENANT ----------------
    for ts, _si, _ti, turn, tenant in ordered:
        if turn.role != TurnRole.AGENT_QUERY:
            continue
        loc = str(turn.turn_id)
        t_tx = ts
        t_v = turn.as_of if turn.as_of is not None else t_tx
        req = set(turn.requires)

        # CONSISTENCY — recall_targets must equal the turn's requires
        rt = gt.recall_targets.get(turn.turn_id)
        if rt != req:
            v.append(Violation(
                "CONSISTENCY",
                f"recall_targets {_fmt(rt)} != turn.requires {_fmt(req)}",
                loc,
            ))

        am = gt.active_memory.get(turn.turn_id, set())

        # V4 — every required fact must be retrievable
        missing = req - am
        if missing:
            v.append(Violation(
                "V4",
                f"required facts not in active_memory "
                f"{[m.hex()[:12] for m in sorted(missing)]}",
                loc,
            ))

        # ACTIVE — soundness of each active_memory entry
        for fid in am:
            d = re_deleted.get(fid)
            if d is not None and d <= t_tx:
                v.append(Violation(
                    "ACTIVE",
                    f"active_memory contains fact {fid.hex()[:12]} deleted "
                    f"at or before query time",
                    loc,
                ))
            intro = introduced_at.get(fid)
            if intro is None or intro > t_tx:
                v.append(Violation(
                    "ACTIVE",
                    f"active_memory contains fact {fid.hex()[:12]} not yet "
                    f"introduced at query time",
                    loc,
                ))
            # Tenant: verifiable only for facts surviving in final state
            # (deleted-later facts are shredded; their tenant is unrecoverable).
            f = final_index.get(fid)
            if f is not None and f.tenant_id != tenant:
                v.append(Violation(
                    "ACTIVE",
                    f"active_memory fact {fid.hex()[:12]} tenant "
                    f"{f.tenant_id!r} != query tenant {tenant!r}",
                    loc,
                ))

    # --- TENANT (W5) — introduces must not cross tenant boundaries --------
    for _ts, _si, _ti, turn, tenant in ordered:
        for fid in turn.introduces:
            f = final_index.get(fid)  # shredded facts can't be checked
            if f is not None and f.tenant_id != tenant:
                v.append(Violation(
                    "TENANT",
                    f"turn (tenant {tenant!r}) introduces fact "
                    f"{fid.hex()[:12]} of tenant {f.tenant_id!r}",
                    str(turn.turn_id),
                ))

    # --- V3 — final-fact chain references resolve; chains terminate -------
    for f in trace.facts:
        if f.superseded_by is not None and f.superseded_by not in final_ids:
            v.append(Violation(
                "V3",
                f"final fact {f.fact_id.hex()[:12]} superseded_by dangling "
                f"reference {f.superseded_by.hex()[:12]}",
                f.fact_id.hex()[:12],
            ))
    # cycle detection via chain walk from each final fact with a successor
    points_to = {
        f.fact_id: f.superseded_by
        for f in trace.facts if f.superseded_by is not None
    }
    for start in points_to:
        seen = {start}
        cur = start
        while cur in points_to:
            cur = points_to[cur]
            if cur in seen:
                v.append(Violation(
                    "V3",
                    f"supersession cycle through {cur.hex()[:12]}",
                    start.hex()[:12],
                ))
                break
            seen.add(cur)

    return v


def validate_trace_strict(trace: Trace) -> None:
    """Raise ValidationFailed on the first violation; return silently if valid."""
    violations = validate_trace(trace)
    if violations:
        raise ValidationFailed(violations[0])


def _fmt(s) -> str:
    if not s:
        return "{}"
    return "{" + ", ".join(sorted(b.hex()[:12] for b in s)) + "}"


# ============================================================================
# Smoke check — run `python validators.py`
# ============================================================================

if __name__ == "__main__":
    from datetime import timezone
    from workloads.w1 import generate_w1  # noqa: E402
    from trace import Difficulty  # noqa: E402

    print("GRAFOMEM validators.py — independent V1-V5 re-checks v0.1.0\n")

    # --- Test 1: clean W1 easy validates ----------------------------------
    tr = generate_w1(seed=0, difficulty=Difficulty.EASY)
    issues = validate_trace(tr)
    assert issues == [], f"clean trace flagged: {[str(i) for i in issues]}"
    print("✓ Clean W1 easy validates            (0 violations)")

    # --- Test 2: clean medium + hard validate -----------------------------
    for diff in (Difficulty.MEDIUM, Difficulty.HARD):
        issues = validate_trace(generate_w1(seed=7, difficulty=diff))
        assert issues == [], f"{diff} flagged: {[str(i) for i in issues]}"
    print("✓ Clean W1 medium + hard validate    (0 violations each)")

    # --- Test 3: corrupt deletion ledger -> V5 ----------------------------
    tr = generate_w1(seed=1, difficulty=Difficulty.EASY)
    phantom = b"\x11" * 16
    tr.ground_truth.deleted_facts[phantom] = datetime(
        2026, 1, 1, tzinfo=timezone.utc,
    )
    rules = {i.rule for i in validate_trace(tr)}
    assert "V5" in rules, f"V5 not caught; got {rules}"
    print("✓ Phantom deletion-ledger entry      (V5 caught)")

    # --- Test 4: corrupt recall_targets -> CONSISTENCY --------------------
    tr = generate_w1(seed=2, difficulty=Difficulty.EASY)
    q_id = next(
        t.turn_id for s in tr.sessions for t in s.turns
        if t.role == TurnRole.AGENT_QUERY
    )
    tr.ground_truth.recall_targets[q_id] = {b"\x22" * 16}
    rules = {i.rule for i in validate_trace(tr)}
    assert "CONSISTENCY" in rules, f"CONSISTENCY not caught; got {rules}"
    print("✓ Tampered recall_targets            (CONSISTENCY caught)")

    # --- Test 5: drop a required fact from active_memory -> V4 -------------
    tr = generate_w1(seed=3, difficulty=Difficulty.EASY)
    q_id = next(
        t.turn_id for s in tr.sessions for t in s.turns
        if t.role == TurnRole.AGENT_QUERY
    )
    tr.ground_truth.active_memory[q_id] = set()  # drop everything
    rules = {i.rule for i in validate_trace(tr)}
    assert "V4" in rules, f"V4 not caught; got {rules}"
    print("✓ Emptied active_memory for a query  (V4 caught)")

    # --- Test 6: dangling superseded_by -> V3 -----------------------------
    tr = generate_w1(seed=4, difficulty=Difficulty.EASY)
    import dataclasses
    tr.facts[0] = dataclasses.replace(tr.facts[0], superseded_by=b"\x33" * 16)
    rules = {i.rule for i in validate_trace(tr)}
    assert "V3" in rules, f"V3 not caught; got {rules}"
    print("✓ Dangling superseded_by pointer     (V3 caught)")

    print("\nAll validator smoke checks green. "
          "V-rule loop closed; corpus-builder can gate on this.")
