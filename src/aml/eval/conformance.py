"""
GRAFOMEM conformance suite — executable realization of GMP §8 (v0.2).

    "A store SUPPORTS capability X iff it PASSES the conformance suite for X."
    — not iff it *declares* X (GMP §8.1).

The suite reads `store.capabilities()`, runs only the tests for declared
capabilities (honest omission is never penalized — anchor B1), and emits a
`ConformanceProfile`: per-capability PASS / FAIL with the oracle-grounded metric
and a bootstrap CI in each direction. The two safety capabilities (HARD_DELETE,
MULTI_TENANT) are tested *two-sided* — leakage AND over-restriction — because the
paper's claim-not-equal-behavior result (soft_delete, leaky_tenant) shows recall
alone cannot detect a leak: a store can hold recall at 1.000 *while* leaking 1.000.

Capability -> workload -> pass condition (GMP §8.3; paper Appendix A):

    AUDIT               constructed   audit() yields everything written
    SUPERSESSION_CHAIN  W2 current     stale-version leakage = 0  AND  recall = 1   (F4)
    BI_TEMPORAL         W2 as_of       historical recall = 1                        (F5)
    HARD_DELETE         W6             deleted leakage = 0  AND  survivor recall = 1 (F10,F11)
    MULTI_TENANT        W5             cross-tenant leakage = 0 AND in-tenant recall=1 (F12,F13)
    PROVENANCE          constructed   source (write_id + written_at) round-trips
    CRYPTOGRAPHIC_PROVENANCE constructed  signed write verifies; altered content does not

A direction PASSES iff its bootstrap CI *excludes the failing outcome* (GMP §8.2),
with a tolerance matched to the direction's nature:

  - LEAKAGE (safety) is exact and embedder-invariant — a forbidden fact is a
    candidate or it is not. Gate: CI upper bound <= EPS.
  - OVER-RESTRICTION (correctness) manifests as a structural *collapse* of recall
    toward zero (F11, F13: 0.000), while a conformant store stays near the
    embedder's ceiling. Because that ceiling is below 1.0 under a weak embedder
    (sub-1% ranking misses on near-identical versions/tenants), the gate detects
    collapse rather than punishing ranking noise: CI lower bound >= REC_FLOOR.
    Under the reference embedder (BGE) conformant recall reaches ~1.000.
  - The AUDIT write/audit invariant is exact and embedder-independent, so it keeps
    the strict recall gate (1 - EPS).

With deterministic per-seed outcomes the CI is degenerate (lo == hi == mean); the
interval is reported for rigor and survives the non-degenerate case. (A tighter,
relative over-restriction test — paired against a same-embedder permissive
baseline, as run_w5/run_w6 do — is a documented refinement for a later version.)

This is a thin orchestration layer: it does not reimplement the oracle, the
harness, or the metrics — it calls them and applies the §8.3 thresholds. The
privacy and supersession directions are embedder-invariant (paper Proposition 2),
so the stub embedder reproduces the PASS/FAIL verdicts; real BGE is for
headline-grade numbers. Requires grafomem[backends].
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from statistics import mean

from aml.backends.interface import (
    Capability,
    ConformanceViolation,
    WriteOptions,
    verify_provenance,
)
from aml.provenance import fact_id_for_content
from aml.eval.harness import run_trace
from aml.eval.metrics import _targets_by_turn, bootstrap_paired_ci
from aml.generator.trace import Difficulty, TxnAnomaly
from aml.generator.workloads.w2 import generate_w2
from aml.generator.workloads.w5 import generate_w5
from aml.generator.workloads.w6 import generate_w6
from aml.generator.workloads.w10 import generate_w10
from aml.eval.concurrency_runner import run_w10

EPS = 5e-3          # leakage gate: exact, embedder-invariant (safety direction)
REC_FLOOR = 0.5     # over-restriction gate: detects collapse, not embedder noise
DEFAULT_SEEDS = range(5)
DEFAULT_BUDGET = 512
DEFAULT_DIFF = Difficulty.HARD

StoreFactory = Callable[[], object]   # () -> a fresh MemoryBackend

__all__ = [
    "run_conformance", "print_profile",
    "ConformanceProfile", "CapabilityResult", "DirectionResult",
]


# ---------------------------------------------------------------------------
# Trace helpers (the same maps run_w5 / run_w6 build, kept local to the suite)
# ---------------------------------------------------------------------------

def _ts_by_turn(trace) -> dict[str, object]:
    return {str(t.turn_id): t.timestamp for s in trace.sessions for t in s.turns}


def _tenant_by_turn(trace) -> dict[str, object]:
    return {str(t.turn_id): s.tenant_id for s in trace.sessions for t in s.turns}


def _turn_by_id(trace) -> dict[str, object]:
    return {str(t.turn_id): t for s in trace.sessions for t in s.turns}


def _superseded_fids(trace) -> set[bytes]:
    return {f.fact_id for f in trace.facts if f.superseded_by is not None}


def _mean_ci(values: Sequence[float]) -> tuple[float, float, float]:
    """One-sample 95% CI of the mean, via the project's paired bootstrap against
    a zero baseline (diffs == values), so we reuse the tested estimator."""
    vals = list(values)
    point, lo, hi = bootstrap_paired_ci([0.0] * len(vals), vals)
    return point, lo, hi


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class DirectionResult:
    name: str                       # e.g. "deleted-fact leakage", "survivor recall"
    objective: str                  # "<= eps" or ">= 1 - eps"
    point: float
    ci: tuple[float, float]
    passed: bool


@dataclass(slots=True)
class CapabilityResult:
    capability: Capability
    workload: str
    directions: list[DirectionResult]

    @property
    def passed(self) -> bool:
        return all(d.passed for d in self.directions)

    @property
    def is_violation(self) -> bool:
        # Every tested capability was declared; a tested-and-failing one is a
        # ConformanceViolation (declared but not honored).
        return not self.passed


@dataclass(slots=True)
class ConformanceProfile:
    store: str
    declared: set[Capability]
    results: list[CapabilityResult]

    @property
    def supported(self) -> set[Capability]:
        return {r.capability for r in self.results if r.passed}

    @property
    def violations(self) -> list[CapabilityResult]:
        return [r for r in self.results if r.is_violation]

    @property
    def untested(self) -> set[Capability]:
        # Declared capabilities with no test (e.g. reserved flags, GMP §7.4).
        return self.declared - {r.capability for r in self.results}

    @property
    def conformance_rate(self) -> float:
        """M8 — fraction of declared capabilities whose conformance test passed.
        Capabilities that are declared but untested (no suite exists yet) are
        excluded from the denominator, so M8 reflects only testable claims.
        Returns 1.0 if no capabilities are tested (vacuously conformant)."""
        tested = [r for r in self.results]  # every result was tested
        if not tested:
            return 1.0
        return sum(1 for r in tested if r.passed) / len(tested)


# ---------------------------------------------------------------------------
# Direction builders
# ---------------------------------------------------------------------------

def _leak_dir(name: str, per_seed: Sequence[float]) -> DirectionResult:
    point, lo, hi = _mean_ci(per_seed)
    return DirectionResult(name, "<= eps", point, (lo, hi), hi <= EPS)


def _recall_dir(name: str, per_seed: Sequence[float], *, floor: float) -> DirectionResult:
    # AUDIT's write/audit invariant is exact (floor = 1 - eps); corpus-recall
    # directions are embedder-sensitive, so they gate on no-collapse (REC_FLOOR):
    # over-restriction craters recall toward 0 (F11, F13), while a conformant
    # store stays near the embedder ceiling (=> 1.000 under the reference model).
    obj = ">= 1 - eps" if floor >= 1.0 - EPS else f">= {floor:.2f} (no collapse)"
    if any(v != v for v in per_seed):              # NaN: no probes -> inconclusive
        return DirectionResult(name, obj, float("nan"),
                               (float("nan"), float("nan")), False)
    point, lo, hi = _mean_ci(per_seed)
    return DirectionResult(name, obj, point, (lo, hi), lo >= floor)


# ---------------------------------------------------------------------------
# Per-capability measurements (single store-under-test)
# ---------------------------------------------------------------------------

def _test_audit(factory: StoreFactory, seeds, budget) -> CapabilityResult:
    """Constructed invariant: every written fact is exposed by audit()."""
    ok = []
    for s in seeds:
        store = factory()
        contents = [f"conformance probe {i} (seed {s})" for i in range(8)]
        for c in contents:
            store.write(c, WriteOptions())
        store.flush()
        audited = {m.content for m in store.audit()}
        ok.append(1.0 if all(c in audited for c in contents) else 0.0)
    return CapabilityResult(Capability.AUDIT, "constructed",
                            [_recall_dir("audit completeness", ok, floor=1.0 - EPS)])


def _test_provenance(factory: StoreFactory, seeds, budget) -> CapabilityResult:
    """Constructed invariant: every written memory carries provenance — a non-None
    source with write_id and written_at — through audit(). A backend that declares
    PROVENANCE but returns source=None (or drops write_id) is in violation."""
    ok = []
    for s in seeds:
        store = factory()
        contents = [f"provenance probe {i} (seed {s})" for i in range(8)]
        for c in contents:
            store.write(c, WriteOptions())
        store.flush()
        contents_set = set(contents)
        memos = [m for m in store.audit() if m.content in contents_set]
        good = len(memos) == len(contents) and all(
            m.source is not None and m.source.write_id is not None
            and m.source.written_at is not None for m in memos)
        ok.append(1.0 if good else 0.0)
    return CapabilityResult(Capability.PROVENANCE, "constructed",
                            [_recall_dir("provenance round-trip", ok, floor=1.0 - EPS)])


def _test_crypto_provenance(factory, seeds, budget):
    """CRYPTOGRAPHIC_PROVENANCE two-sided: a signed write verifies against its content
    fact_id (validity), and a fact_id for ALTERED content does NOT (tamper rejection —
    the signature binds to the exact bytes). Crypto dep is touched only here."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat, PublicFormat,
    )
    valid_ps, tamper_ps = [], []
    for s in seeds:
        store = factory()
        key = Ed25519PrivateKey.generate().private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        
        class _MockId:
            def __init__(self, k): self.k = k
            def sign(self, m): 
                priv = Ed25519PrivateKey.from_private_bytes(self.k)
                return priv.sign(m), priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            def public_key(self):
                return Ed25519PrivateKey.from_private_bytes(self.k).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

        contents = [f"signed probe {i} (seed {s})" for i in range(8)]
        for c in contents:
            store.write(c, WriteOptions(signing_identity=_MockId(key)))
        store.flush()
        valid, tamper = [], []
        contents_set = set(contents)
        for m in store.audit():
            if m.content not in contents_set:
                continue
            valid.append(1.0 if verify_provenance(
                m, fact_id_for_content(m.content, m.tenant_id)) else 0.0)
            tamper.append(1.0 if verify_provenance(
                m, fact_id_for_content(m.content + "X", m.tenant_id)) else 0.0)
        valid_ps.append(mean(valid) if valid else 0.0)
        tamper_ps.append(mean(tamper) if tamper else 0.0)
    return valid_ps, tamper_ps


def _measure_w2_current(factory, seeds, budget):
    """SUPERSESSION_CHAIN: on current (as_of=None) queries, the answer set must be
    heads only — no superseded version may appear — and the head must be recalled."""
    stale_ps, rec_ps = [], []
    for s in seeds:
        tr = generate_w2(seed=s, difficulty=DEFAULT_DIFF)
        tgt, turns, superseded = _targets_by_turn(tr), _turn_by_id(tr), _superseded_fids(tr)
        run = run_trace(factory(), tr, budget_tokens=budget)
        stale, rec = [], []
        for qr in run.per_query:
            if turns[qr.turn_id].as_of is not None:    # current queries only
                continue
            stale.append(1.0 if (qr.retrieved & superseded) else 0.0)
            T = tgt.get(qr.turn_id, set())
            if T:
                rec.append(len(qr.retrieved & T) / len(T))
        stale_ps.append(mean(stale) if stale else 0.0)
        rec_ps.append(mean(rec) if rec else float("nan"))
    return stale_ps, rec_ps


def _measure_w2_historical(factory, seeds, budget):
    """BI_TEMPORAL: as_of queries resolve to the version valid at t (recall = 1)."""
    rec_ps = []
    for s in seeds:
        tr = generate_w2(seed=s, difficulty=DEFAULT_DIFF)
        tgt, turns = _targets_by_turn(tr), _turn_by_id(tr)
        run = run_trace(factory(), tr, budget_tokens=budget)
        rec = []
        for qr in run.per_query:
            if turns[qr.turn_id].as_of is None:        # historical queries only
                continue
            T = tgt.get(qr.turn_id, set())
            if T:
                rec.append(len(qr.retrieved & T) / len(T))
        rec_ps.append(mean(rec) if rec else float("nan"))
    return rec_ps


def _measure_w6(factory, seeds, budget):
    """HARD_DELETE two-sided (generalizes run_w6.measure to one store)."""
    leak_ps, surv_ps = [], []
    for s in seeds:
        tr = generate_w6(seed=s, difficulty=DEFAULT_DIFF)
        tgt, tsb = _targets_by_turn(tr), _ts_by_turn(tr)
        deleted = tr.ground_truth.deleted_facts       # {fid: deleted_at}
        run = run_trace(factory(), tr, budget_tokens=budget)
        leak, surv = [], []
        for qr in run.per_query:
            ts = tsb[qr.turn_id]
            deleted_before = {fid for fid, dt in deleted.items() if dt <= ts}
            leak.append(1.0 if (qr.retrieved & deleted_before) else 0.0)
            T = tgt.get(qr.turn_id, set())
            if T:
                surv.append(len(qr.retrieved & T) / len(T))
        leak_ps.append(mean(leak))
        surv_ps.append(mean(surv) if surv else float("nan"))
    return leak_ps, surv_ps


def _measure_w5(factory, seeds, budget):
    """MULTI_TENANT two-sided (generalizes run_w5.measure to one store)."""
    leak_ps, rec_ps = [], []
    for s in seeds:
        tr = generate_w5(seed=s, difficulty=DEFAULT_DIFF)
        tgt, q_tenant = _targets_by_turn(tr), _tenant_by_turn(tr)
        fid_tenant = {f.fact_id: f.tenant_id for f in tr.facts}
        run = run_trace(factory(), tr, budget_tokens=budget)
        leak, rec = [], []
        for qr in run.per_query:
            qt = q_tenant[qr.turn_id]
            cross = any(fid_tenant.get(fid) != qt for fid in qr.retrieved)
            leak.append(1.0 if cross else 0.0)
            T = tgt.get(qr.turn_id, set())
            if T:
                rec.append(len(qr.retrieved & T) / len(T))
        leak_ps.append(mean(leak))
        rec_ps.append(mean(rec) if rec else float("nan"))
    return leak_ps, rec_ps


def _measure_w10(factory, seeds, budget):
    """CONCURRENCY_CONTROL two-sided (generalizes run_w10.measure to one store):
    the isolation DOWNGRADE rate over the lattice probes (the over-claim
    direction — achieved level weaker than declared) and the §10.4 RESURRECTION
    rate over the durable-delete probes. Both should be 0 for a conformant store.
    `budget` is unused — W10 stores are synthetic (no retrieval/embedder)."""
    dg_ps, res_ps = [], []
    for s in seeds:
        tr = generate_w10(seed=s, difficulty=DEFAULT_DIFF)
        verdicts = run_w10(factory(), tr)
        lattice = [v for v in verdicts if v.anomaly is not TxnAnomaly.RESURRECTION]
        resur = [v for v in verdicts if v.anomaly is TxnAnomaly.RESURRECTION]
        dg_ps.append(mean([0.0 if v.status == "OK" else 1.0 for v in lattice])
                     if lattice else float("nan"))
        res_ps.append(mean([0.0 if v.status == "OK" else 1.0 for v in resur])
                      if resur else float("nan"))
    return dg_ps, res_ps


# ---------------------------------------------------------------------------
# The suite
# ---------------------------------------------------------------------------

def run_conformance(
    store_factory: StoreFactory,
    *,
    name: str | None = None,
    seeds=DEFAULT_SEEDS,
    budget: int = DEFAULT_BUDGET,
    strict: bool = False,
) -> ConformanceProfile:
    """Run the GMP conformance suite against `store_factory` (a callable
    returning a fresh store, as in run_w5/run_w6's `mk()`). Tests only declared
    capabilities; raises ConformanceViolation at the end if `strict` and any
    declared capability fails its suite."""
    seeds = list(seeds)
    probe = store_factory()
    declared = set(probe.capabilities())
    name = name or type(probe).__name__

    results: list[CapabilityResult] = []

    if Capability.AUDIT in declared:
        results.append(_test_audit(store_factory, seeds, budget))

    if Capability.SUPERSESSION_CHAIN in declared:
        stale, rec = _measure_w2_current(store_factory, seeds, budget)
        results.append(CapabilityResult(
            Capability.SUPERSESSION_CHAIN, "W2 current",
            [_leak_dir("stale-version leakage", stale), _recall_dir("current recall", rec, floor=REC_FLOOR)]))

    if Capability.BI_TEMPORAL in declared:
        rec = _measure_w2_historical(store_factory, seeds, budget)
        results.append(CapabilityResult(
            Capability.BI_TEMPORAL, "W2 as_of",
            [_recall_dir("historical recall", rec, floor=REC_FLOOR)]))

    if Capability.HARD_DELETE in declared:
        leak, surv = _measure_w6(store_factory, seeds, budget)
        results.append(CapabilityResult(
            Capability.HARD_DELETE, "W6",
            [_leak_dir("deleted-fact leakage", leak), _recall_dir("survivor recall", surv, floor=REC_FLOOR)]))

    if Capability.MULTI_TENANT in declared:
        leak, rec = _measure_w5(store_factory, seeds, budget)
        results.append(CapabilityResult(
            Capability.MULTI_TENANT, "W5",
            [_leak_dir("cross-tenant leakage", leak), _recall_dir("in-tenant recall", rec, floor=REC_FLOOR)]))

    if Capability.CONCURRENCY_CONTROL in declared:
        # The suite verifies behavior against the store's CLAIM, so it needs the
        # store's declared isolation policy. A store that declares the capability
        # but does not expose `declared_policy` cannot be checked -> non-conformant.
        if getattr(probe, "declared_policy", None) is None:
            results.append(CapabilityResult(
                Capability.CONCURRENCY_CONTROL, "W10",
                [DirectionResult("declared policy exposed",
                                 "store exposes declared_policy",
                                 0.0, (0.0, 0.0), False)]))
        else:
            dg, res = _measure_w10(store_factory, seeds, budget)
            results.append(CapabilityResult(
                Capability.CONCURRENCY_CONTROL, "W10",
                [_leak_dir("isolation downgrade (over-claim)", dg),
                 _leak_dir("delete resurrection (§10.4)", res)]))

    if Capability.PROVENANCE in declared:
        results.append(_test_provenance(store_factory, seeds, budget))

    if Capability.CRYPTOGRAPHIC_PROVENANCE in declared:
        valid, tamper = _test_crypto_provenance(store_factory, seeds, budget)
        results.append(CapabilityResult(
            Capability.CRYPTOGRAPHIC_PROVENANCE, "constructed",
            [_recall_dir("signature validity", valid, floor=1.0 - EPS),
             _leak_dir("tamper acceptance", tamper)]))

    profile = ConformanceProfile(name, declared, results)

    if strict and profile.violations:
        bad = ", ".join(r.capability.value for r in profile.violations)
        raise ConformanceViolation(
            f"{name}: declares but fails conformance for: {bad}")
    return profile


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_profile(profile: ConformanceProfile) -> None:
    print(f"\n=== {profile.store} ===")
    print(f"  declared: {{{', '.join(sorted(c.value for c in profile.declared))}}}")
    if not profile.results:
        print("  (no declared capability has a v0.1 conformance test)")
    for r in profile.results:
        verdict = "PASS" if r.passed else ("VIOLATION" if r.is_violation else "FAIL")
        print(f"  {r.capability.value:20s} [{r.workload:11s}]  {verdict}")
        for d in r.directions:
            lo, hi = d.ci
            mark = "ok " if d.passed else "XX "
            print(f"      {mark}{d.name:24s} {d.point:6.3f}  "
                  f"95% CI [{lo:+.3f}, {hi:+.3f}]  ({d.objective})")
    sup = ", ".join(sorted(c.value for c in profile.supported)) or "(none)"
    print(f"  -> SUPPORTS: {{{sup}}}")
    if profile.violations:
        v = ", ".join(r.capability.value for r in profile.violations)
        print(f"  -> CONFORMANCE VIOLATIONS (declared, not honored): {{{v}}}")
    if profile.untested:
        u = ", ".join(sorted(c.value for c in profile.untested))
        print(f"  -> declared but no v0.1 test: {{{u}}}")


# ============================================================================
# Smoke / demo — run `python -m aml.eval.conformance`
#
# Runs the suite against every reference backend with the STUB embedder (the
# privacy and supersession directions are embedder-invariant, paper Prop 2, so
# the verdicts match real BGE). Expected: honest_delete / tenant_scoped /
# bi_temporal / supersession_chain PASS their claims; soft_delete and
# leaky_tenant declare the safety flag and are flagged as VIOLATIONS — the
# paper's claim-not-equal-behavior result, now an automated verdict.
# ============================================================================

if __name__ == "__main__":
    from aml.backends.vector_only import VectorOnlyBackend, _stub_embedder
    from aml.backends.supersession_chain import SupersessionVectorBackend
    from aml.backends.bi_temporal import BiTemporalVectorBackend
    from aml.backends.delete_backends import (
        CoarseDeleteBackend, HonestDeleteBackend, SoftDeleteBackend,
    )
    from aml.backends.tenant_backends import (
        LeakyTenant, OverIsolating, TenantScoped,
    )
    from aml.backends.isolation_backends import (
        NoIsolationStore, ReadCommittedStore, ResurrectingStore,
        SerializableStore, SnapshotStore,
    )

    print("GRAFOMEM conformance suite — GMP v0.1 §8 (STUB embedder)\n"
          "supported = passes the suite, NOT declares the flag.")

    emb = _stub_embedder()
    stores: list[tuple[str, StoreFactory]] = [
        ("vector_only",                       lambda: VectorOnlyBackend(embed_fn=emb)),
        ("supersession_chain",                lambda: SupersessionVectorBackend(embed_fn=emb)),
        ("bi_temporal",                       lambda: BiTemporalVectorBackend(embed_fn=emb)),
        ("honest_delete",                     lambda: HonestDeleteBackend(embed_fn=emb)),
        ("soft_delete (claims HARD_DELETE)",  lambda: SoftDeleteBackend(embed_fn=emb)),
        ("coarse_delete",                     lambda: CoarseDeleteBackend(embed_fn=emb)),
        ("tenant_scoped",                     lambda: TenantScoped(embed_fn=emb)),
        ("leaky_tenant (claims MULTI_TENANT)", lambda: LeakyTenant(embed_fn=emb)),
        ("over_isolating",                    lambda: OverIsolating(embed_fn=emb)),
        ("serializable_store",                       SerializableStore),
        ("snapshot_store",                           SnapshotStore),
        ("read_committed_store",                     ReadCommittedStore),
        ("no_isolation_store (claims SERIALIZABLE)", NoIsolationStore),
        ("resurrecting_store (claims SERIALIZABLE)", ResurrectingStore),
    ]
    profiles = [run_conformance(mk, name=nm) for nm, mk in stores]
    for p in profiles:
        print_profile(p)

    # The headline: the two backends that *declare* a safety capability and leak
    # are exactly the ones flagged as violations — recall never betrayed them.
    violators = {p.store.split()[0] for p in profiles if p.violations}
    assert "soft_delete" in violators, "soft_delete should be a HARD_DELETE violation"
    assert "leaky_tenant" in violators, "leaky_tenant should be a MULTI_TENANT violation"
    assert "no_isolation_store" in violators, \
        "no_isolation should be a CONCURRENCY_CONTROL over-claim violation"
    assert "resurrecting_store" in violators, \
        "resurrecting should be a §10.4 durability violation"
    assert "serializable_store" not in violators, \
        "serializable_store honors its claim and should PASS CONCURRENCY_CONTROL"
    print("\n✓ Claim != behavior is caught: soft_delete and leaky_tenant declare "
          "the\n  safety flag, pass the type contract, and are flagged VIOLATIONS.")
    print("✓ Same for concurrency: no_isolation_store (claims serializable, delivers\n"
          "  read_committed) and resurrecting_store (§10.4) are CONCURRENCY_CONTROL\n"
          "  VIOLATIONS; the three honest stores pass at their declared level.")
    print("\nConformance suite green. This is the executable meaning of "
          "\"supports X\" in GMP §8.")
