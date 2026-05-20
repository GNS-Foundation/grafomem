"""
GRAFOMEM workload W1 — Stable Recall.

See 01-workload-spec.md §4.1. Purpose: baseline isolation of pure retrieval
at varying horizons. N facts (all valid_from = T0, no valid_until) are
introduced across S sessions; each fact is queried once per horizon, placed
H positions after its introduction. Each query requires exactly one fact,
and every (subject, predicate) pair is unique so queries are unambiguous.

Determinism (R1): all randomness flows from a single RNG seeded by
(workload, seed, difficulty) via a stable BLAKE2b hash — reproducible across
machines. All UUIDs are drawn from that RNG, so a given (seed, difficulty)
yields a byte-identical trace modulo `generated_at`.

This module is the first to exercise the oracle on non-toy input.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from aml.generator.trace import (
    Difficulty, Fact, Session, Trace, Turn, TurnRole, Workload,
)
from aml.generator.oracle import derive_ground_truth


# ============================================================================
# Difficulty parameters (§4.1)
# ============================================================================

@dataclass(frozen=True, slots=True)
class _W1Params:
    n_facts: int
    n_sessions: int
    horizons: tuple[int, ...]


_W1_PARAMS: dict[Difficulty, _W1Params] = {
    Difficulty.EASY:   _W1Params(n_facts=20,  n_sessions=1,  horizons=(10,)),
    Difficulty.MEDIUM: _W1Params(n_facts=100, n_sessions=5,  horizons=(10, 100)),
    Difficulty.HARD:   _W1Params(n_facts=500, n_sessions=20, horizons=(10, 100, 1000)),
}


# ============================================================================
# Synthetic entity population (§3.6) — never real-world PII.
# Invented names for identifying categories (persons/places/orgs);
# generic category nouns for non-identifying ones (things/languages/allergens).
# ============================================================================

_PERSONS = [
    "Aria", "Bram", "Caius", "Dela", "Enzo", "Fira", "Goran", "Hesper",
    "Ilse", "Jaro", "Kesia", "Lorin", "Mira", "Nael", "Orsa", "Pell",
    "Quill", "Rhea", "Sefa", "Tarn", "Ulla", "Vido", "Wren", "Xander",
    "Yara", "Zane", "Adra", "Belan", "Coris", "Dyla", "Esmu", "Faron",
    "Gita", "Holt", "Ivar", "Juna", "Kavi", "Lysa", "Mott", "Nira",
    "Okin", "Pria", "Roan", "Sela",
]
_PLACES = [
    "Aldermoor", "Brightvale", "Cinderhollow", "Dunmere", "Everwick",
    "Frosthaven", "Glenmoor", "Highford", "Ironvale", "Junewood",
    "Karnesh", "Lowmere", "Marrowfen", "Northgate", "Oakhollow",
    "Pinecrest", "Quarryhill", "Redmarsh", "Stonewick", "Thornvale",
    "Umberfield", "Vexholm", "Westmere", "Yarrowdale",
]
_ORGS = [
    "Acuna Labs", "Borealis Group", "Cobalt Works", "Drayton Corp",
    "Ember Systems", "Fenwick Trust", "Granite Holdings", "Helix Foundry",
    "Ionis Partners", "Juniper Mills", "Kestrel Bank", "Lumen Industries",
    "Meridian Co", "Norgrove LLC", "Oryx Ventures", "Pallas Group",
]
_THINGS = [
    "espresso", "vinyl records", "hiking boots", "old maps", "green tea",
    "fountain pens", "chess sets", "wool blankets", "sourdough bread",
    "mountain bikes", "jazz albums", "ceramic mugs", "linen shirts",
    "board games", "camping gear", "film cameras", "houseplants",
    "leather journals", "bonsai trees", "trail running",
]
_LANGUAGES = [
    "Italian", "Spanish", "Mandarin", "French", "German", "Japanese",
    "Portuguese", "Arabic", "Hindi", "Korean", "Dutch", "Swedish",
    "Greek", "Turkish", "Polish", "Vietnamese",
]
_ALLERGENS = [
    "peanuts", "shellfish", "pollen", "dairy", "gluten", "tree nuts",
    "soy", "eggs", "dust mites", "penicillin", "latex", "bee stings",
    "sesame", "mustard",
]

_POOLS = {
    "PLACES": _PLACES, "ORGS": _ORGS, "THINGS": _THINGS,
    "LANGUAGES": _LANGUAGES, "ALLERGENS": _ALLERGENS, "PERSONS": _PERSONS,
}

# predicate -> (object pool name, statement template, question template)
_PREDICATES: dict[str, tuple[str, str, str]] = {
    "lives_in":    ("PLACES",    "{s} lives in {o}.",            "Where does {s} live?"),
    "born_in":     ("PLACES",    "{s} was born in {o}.",         "Where was {s} born?"),
    "located_at":  ("PLACES",    "{s} is currently at {o}.",     "Where is {s} located?"),
    "visits":      ("PLACES",    "{s} often visits {o}.",        "Where does {s} visit?"),
    "works_at":    ("ORGS",      "{s} works at {o}.",            "Where does {s} work?"),
    "member_of":   ("ORGS",      "{s} is a member of {o}.",      "What is {s} a member of?"),
    "manages":     ("ORGS",      "{s} manages {o}.",             "What does {s} manage?"),
    "prefers":     ("THINGS",    "{s} prefers {o}.",             "What does {s} prefer?"),
    "dislikes":    ("THINGS",    "{s} dislikes {o}.",            "What does {s} dislike?"),
    "recommends":  ("THINGS",    "{s} recommends {o}.",          "What does {s} recommend?"),
    "avoids":      ("THINGS",    "{s} avoids {o}.",              "What does {s} avoid?"),
    "owns":        ("THINGS",    "{s} owns {o}.",                "What does {s} own?"),
    "speaks":      ("LANGUAGES", "{s} speaks {o}.",              "What language does {s} speak?"),
    "allergic_to": ("ALLERGENS", "{s} is allergic to {o}.",      "What is {s} allergic to?"),
    "married_to":  ("PERSONS",   "{s} is married to {o}.",       "Who is {s} married to?"),
    "parent_of":   ("PERSONS",   "{s} is the parent of {o}.",    "Who is {s} the parent of?"),
    "knows":       ("PERSONS",   "{s} knows {o}.",               "Who does {s} know?"),
    "employs":     ("PERSONS",   "{s} employs {o}.",             "Who does {s} employ?"),
}


# ============================================================================
# Deterministic RNG + UUIDs
# ============================================================================

# Use a private Random instance, not the global module, so generation is
# isolated and reproducible.
import random  # noqa: E402


def _make_rng(workload: Workload, seed: int, difficulty: Difficulty) -> random.Random:
    """Seed a private RNG from a stable cross-machine hash of the parameters."""
    key = f"{workload.value}|{seed}|{difficulty.value}".encode("utf-8")
    digest = hashlib.blake2b(key, digest_size=8).digest()
    return random.Random(int.from_bytes(digest, "big"))


def _det_uuid(rng: random.Random) -> UUID:
    """A deterministic UUID drawn from the seeded RNG (format-valid; the
    version/variant bits are not RFC-4122 compliant, which is fine — the
    schema only checks the hex layout)."""
    return UUID(int=rng.getrandbits(128))


# ============================================================================
# Generation
# ============================================================================

def _sample_facts(rng: random.Random, n: int, t0: datetime) -> list[Fact]:
    """Sample n facts with unique (subject, predicate) pairs so each query
    is unambiguous. All facts share valid_from = t0, no valid_until."""
    predicates = list(_PREDICATES.keys())
    # Enumerate all (subject, predicate) pairs, shuffle deterministically.
    pairs = [(subj, pred) for subj in _PERSONS for pred in predicates]
    if n > len(pairs):
        raise ValueError(
            f"requested {n} facts but only {len(pairs)} unique "
            f"(subject, predicate) pairs available; enlarge the population"
        )
    rng.shuffle(pairs)
    chosen = pairs[:n]

    facts: list[Fact] = []
    for i, (subject, predicate) in enumerate(chosen):
        pool_name, _, _ = _PREDICATES[predicate]
        pool = _POOLS[pool_name]
        obj = rng.choice(pool)
        # For person-valued predicates, avoid object == subject.
        if pool_name == "PERSONS":
            while obj == subject:
                obj = rng.choice(pool)
        facts.append(Fact(
            predicate=predicate,
            subject=subject,
            object=obj,
            valid_from=t0,
            sequence=i + 1,        # monotonic, consistent with intro order
            importance=1.0,        # W1: all facts equally important
        ))
    return facts


def _statement(fact: Fact) -> str:
    _, stmt, _ = _PREDICATES[fact.predicate]
    return stmt.format(s=fact.subject, o=fact.object)


def _question(fact: Fact) -> str:
    _, _, q = _PREDICATES[fact.predicate]
    return q.format(s=fact.subject)


def _build_turns(
    rng: random.Random,
    facts: list[Fact],
    horizons: tuple[int, ...],
    t0: datetime,
) -> list[Turn]:
    """Build the turn timeline: fact i is introduced at position i; each
    fact is queried at position (i + h) for every horizon h. Events are
    sorted by (position, intro-before-query, fact_index), then assigned
    sequential 1-second timestamps."""
    # (position, is_query, fact_index)
    events: list[tuple[int, int, int]] = []
    for i in range(len(facts)):
        events.append((i, 0, i))              # introduction
        for h in horizons:
            events.append((i + h, 1, i))      # query at horizon h
    events.sort()

    turns: list[Turn] = []
    for k, (_pos, is_query, fi) in enumerate(events):
        ts = t0 + timedelta(seconds=k)
        fact = facts[fi]
        if is_query == 0:
            turns.append(Turn(
                turn_id=_det_uuid(rng),
                role=TurnRole.USER,
                content=_statement(fact),
                content_template=_statement(fact),
                timestamp=ts,
                introduces=[fact.fact_id],
            ))
        else:
            turns.append(Turn(
                turn_id=_det_uuid(rng),
                role=TurnRole.AGENT_QUERY,
                content=_question(fact),
                content_template=_question(fact),
                timestamp=ts,
                requires=[fact.fact_id],
                # W1 has no temporal drift; queries are current-state.
                as_of=None,
            ))
    return turns


def _split_into_sessions(
    rng: random.Random,
    turns: list[Turn],
    n_sessions: int,
) -> list[Session]:
    """Split the sorted turn list into n_sessions contiguous blocks. Each
    block stays in timestamp order, and earlier sessions hold earlier turns,
    keeping (timestamp, session_index, turn_index) ordering consistent."""
    sessions: list[Session] = []
    chunk = math.ceil(len(turns) / n_sessions)
    for i in range(n_sessions):
        block = turns[i * chunk:(i + 1) * chunk]
        if not block:
            continue
        sessions.append(Session(
            session_id=_det_uuid(rng),
            start_time=block[0].timestamp,
            end_time=block[-1].timestamp,
            turns=block,
            tenant_id=None,        # W1 is single-tenant
        ))
    return sessions


def generate_w1(seed: int, difficulty: Difficulty) -> Trace:
    """Generate a complete W1 trace: facts, sessions, and oracle-derived
    ground truth, assembled into a validated Trace."""
    params = _W1_PARAMS[difficulty]
    rng = _make_rng(Workload.W1, seed, difficulty)
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    facts = _sample_facts(rng, params.n_facts, t0)
    turns = _build_turns(rng, facts, params.horizons, t0)
    sessions = _split_into_sessions(rng, turns, params.n_sessions)

    result = derive_ground_truth(facts, sessions)

    return Trace(
        trace_id=_det_uuid(rng),
        workload=Workload.W1,
        difficulty=difficulty,
        seed=seed,
        facts=result.final_facts,
        sessions=sessions,
        ground_truth=result.ground_truth,
    )


# ============================================================================
# Smoke check — run `python workloads/w1.py`
# ============================================================================

if __name__ == "__main__":
    import json
    import time

    from aml.generator.trace import trace_to_dict, validate_trace_schema

    print("GRAFOMEM workloads/w1.py — Stable Recall generator\n")

    def stats(trace: Trace) -> tuple[int, int, int]:
        n_turns = sum(len(s.turns) for s in trace.sessions)
        n_queries = sum(
            1 for s in trace.sessions for t in s.turns
            if t.role == TurnRole.AGENT_QUERY
        )
        return len(trace.facts), n_turns, n_queries

    # --- Test 1: generate easy, basic structure ---------------------------
    tr = generate_w1(seed=0, difficulty=Difficulty.EASY)
    nf, nt, nq = stats(tr)
    assert nf == 20, f"expected 20 facts, got {nf}"
    assert nq == 20, f"expected 20 queries (1 horizon), got {nq}"
    # Every query's recall_target is exactly its one required fact, and that
    # fact is in active_memory (oracle's V4 check already guaranteed this).
    for s in tr.sessions:
        for t in s.turns:
            if t.role == TurnRole.AGENT_QUERY:
                rt = tr.ground_truth.recall_targets[t.turn_id]
                am = tr.ground_truth.active_memory[t.turn_id]
                assert rt == set(t.requires)
                assert rt <= am
    print(f"✓ Generates W1 easy                  "
          f"({nf} facts, {nt} turns, {nq} queries, "
          f"{len(tr.sessions)} session)")

    # --- Test 2: determinism (R1) -----------------------------------------
    a = trace_to_dict(generate_w1(seed=42, difficulty=Difficulty.EASY))
    b = trace_to_dict(generate_w1(seed=42, difficulty=Difficulty.EASY))
    a.pop("generated_at"); b.pop("generated_at")  # wall-clock, excluded
    assert a == b, "same (seed, difficulty) produced different traces"
    # Different seed -> different trace.
    c = trace_to_dict(generate_w1(seed=43, difficulty=Difficulty.EASY))
    c.pop("generated_at")
    assert a != c, "different seeds produced identical traces"
    print(f"✓ Deterministic across runs (R1)     "
          f"(seed 42 byte-identical; seed 43 differs)")

    # --- Test 3: schema validation ----------------------------------------
    validate_trace_schema(trace_to_dict(tr))
    print(f"✓ JSON-Schema validation passed      "
          f"(generated trace conforms to v0.1.2)")

    # --- Test 4: round-trip ------------------------------------------------
    from aml.generator.trace import trace_from_dict
    d = trace_to_dict(tr)
    restored = trace_from_dict(json.loads(json.dumps(d)))
    assert {f.fact_id for f in restored.facts} == {f.fact_id for f in tr.facts}
    print(f"✓ Round-trip serialization clean     "
          f"(facts + ground truth survive JSON)")

    # --- Test 5: medium + hard generate cleanly (oracle V4 holds) ---------
    t_med = generate_w1(seed=1, difficulty=Difficulty.MEDIUM)
    mf, mt, mq = stats(t_med)
    assert mf == 100 and mq == 200  # 100 facts x 2 horizons
    print(f"✓ Generates W1 medium                "
          f"({mf} facts, {mt} turns, {mq} queries, "
          f"{len(t_med.sessions)} sessions)")

    t_start = time.perf_counter()
    t_hard = generate_w1(seed=2, difficulty=Difficulty.HARD)
    elapsed = time.perf_counter() - t_start
    hf, ht, hq = stats(t_hard)
    assert hf == 500 and hq == 1500  # 500 facts x 3 horizons
    validate_trace_schema(trace_to_dict(t_hard))
    print(f"✓ Generates W1 hard                  "
          f"({hf} facts, {ht} turns, {hq} queries, "
          f"{len(t_hard.sessions)} sessions, {elapsed:.2f}s)")

    print(f"\nAll W1 smoke checks green. "
          f"First end-to-end: generate -> derive -> validate -> serialize.")
