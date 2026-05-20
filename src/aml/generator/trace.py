"""
GRAFOMEM trace data model — v0.1.2.

Implements the data types defined in 01-workload-spec.md v0.1.2:
    - Fact (§3.2) — content-addressed via BLAKE2b-128, with Lamport sequence
    - Session (§3.3)
    - Turn (§3.4) — with introduces / deletes / requires / as_of
    - GroundTruth (§3.5) — the derived oracle structure
    - Trace (§3.1) — the top-level corpus unit
    - ParaphraseMeta (§6)

v0.1.2 adds Turn.as_of: the valid-time of an agent_query turn, passed to
backend retrieve(as_of=...). Required to express W2 pre-supersession queries.

Plus:
    - Canonical content-addressing (compute_fact_id) per §3.2
    - JSON serialization with microsecond datetime and hex byte encoding
    - JSON-Schema validator for on-disk traces

Does NOT implement (separate modules):
    - Workload generation procedures   -> workloads/w1.py, w2.py, ...
    - GroundTruth derivation            -> oracle.py
    - Semantic validation V1-V5         -> validators.py

External dependencies: jsonschema (>= 4.0).
Python: 3.12+.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

try:
    import jsonschema
except ImportError as e:
    jsonschema = None  # validation will warn and skip if unavailable


# ============================================================================
# Constants
# ============================================================================

SCHEMA_VERSION: str = "0.1.2"
GENERATOR_VERSION: str = "0.1.0"

FACT_ID_BYTES: int = 16  # BLAKE2b-128 digest
UNIT_SEP: bytes = b"\x1f"  # ASCII unit separator for canonical concatenation

# Controlled predicate vocabulary, frozen at v0.1.x per §3.6.
# Extending requires a schema version bump.
CONTROLLED_PREDICATES: frozenset[str] = frozenset({
    "lives_in", "works_at", "prefers", "owns", "dislikes",
    "allergic_to", "speaks", "born_in", "married_to", "parent_of",
    "employs", "manages", "visits", "recommends", "avoids",
    "knows", "member_of", "located_at", "costs", "scheduled_for",
})


# ============================================================================
# Enums
# ============================================================================

class Workload(StrEnum):
    W1 = "W1"  # Stable Recall
    W2 = "W2"  # Drift & Conflict
    W3 = "W3"  # Distractor Noise
    W4 = "W4"  # Long-Horizon Dependencies
    W5 = "W5"  # Multi-Tenant Isolation
    W6 = "W6"  # Concurrent Updates


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class TurnRole(StrEnum):
    USER = "user"
    AGENT_QUERY = "agent_query"
    AGENT_RESPONSE = "agent_response"


# ============================================================================
# Helpers
# ============================================================================

def _canonical_object_bytes(obj: str | int | float | bool) -> bytes:
    """Canonical byte encoding of a fact object for content-addressing.

    Order of isinstance checks matters: bool is a subclass of int in Python,
    so we must check bool first to avoid encoding True/False as "1"/"0".
    """
    if isinstance(obj, bool):
        return b"true" if obj else b"false"
    elif isinstance(obj, int):
        return str(obj).encode("utf-8")
    elif isinstance(obj, float):
        # repr() gives the shortest round-trip representation in Python 3.
        return repr(obj).encode("utf-8")
    elif isinstance(obj, str):
        return obj.encode("utf-8")
    else:
        raise TypeError(
            f"Unsupported Fact.object type: {type(obj).__name__}. "
            f"Allowed: str | int | float | bool."
        )


def _format_iso_microsecond(dt: datetime) -> str:
    """Format a datetime as ISO8601 with microsecond precision.

    Requires timezone-aware datetime. Naive datetimes are rejected by Fact
    construction (§3.2 mandates microsecond precision; we additionally
    require explicit UTC offset for unambiguous serialization).
    """
    if dt.tzinfo is None:
        raise ValueError(
            "datetime must be timezone-aware (use timezone.utc or equivalent)"
        )
    return dt.isoformat(timespec="microseconds")


def _parse_iso_microsecond(s: str) -> datetime:
    """Parse an ISO8601 microsecond-precision datetime string."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {s!r}")
    return dt


def compute_fact_id(
    predicate: str,
    subject: str,
    object: str | int | float | bool,  # noqa: A002 — spec field name
    valid_from: datetime,
) -> bytes:
    """Compute the BLAKE2b-128 content hash of a canonical fact tuple.

    Per 01-workload-spec.md §3.2:
        fact_id = BLAKE2b-128(predicate || sep || subject || sep
                              || canonical_object || sep || valid_from_iso)

    The unit-separator delimiter prevents collisions between fields like
    ("ab", "cd") and ("a", "bcd"); the canonical object encoding ensures
    type-preservation (1 and 1.0 produce distinct hashes).
    """
    parts = [
        predicate.encode("utf-8"),
        subject.encode("utf-8"),
        _canonical_object_bytes(object),
        _format_iso_microsecond(valid_from).encode("utf-8"),
    ]
    h = hashlib.blake2b(digest_size=FACT_ID_BYTES)
    h.update(UNIT_SEP.join(parts))
    return h.digest()


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass(slots=True)
class ParaphraseMeta:
    """Records the LLM paraphrase layer's configuration, when applied (§6)."""
    model_id: str
    prompt_version: str
    temperature: float
    cache_key_scheme: str


@dataclass(slots=True)
class Fact:
    """A single fact in the world-state. Content-addressed via fact_id.

    See 01-workload-spec.md §3.2.

    `fact_id` is computed automatically if omitted at construction. If
    provided (e.g., during deserialization), it is verified against the
    canonical hash of the other fields; a mismatch raises ValueError.

    `sequence` is metadata for ordering, NOT part of the hash.
    """
    predicate: str
    subject: str
    object: str | int | float | bool  # noqa: A003 — spec field name
    valid_from: datetime
    sequence: int
    importance: float = 1.0
    valid_until: datetime | None = None
    superseded_by: bytes | None = None
    tenant_id: str | None = None
    source_turn_id: UUID | None = None
    fact_id: bytes = field(default=b"")  # set in __post_init__ if empty

    def __post_init__(self) -> None:
        # Validate controlled-vocab predicate (§3.6)
        if self.predicate not in CONTROLLED_PREDICATES:
            raise ValueError(
                f"predicate {self.predicate!r} not in controlled vocabulary; "
                f"see 01-workload-spec.md §3.6"
            )
        # Validate importance range (§3.2)
        if not (0.0 <= self.importance <= 1.0):
            raise ValueError(
                f"importance must be in [0, 1]; got {self.importance}"
            )
        # Validate sequence > 0 (Lamport clock starts at 1)
        if self.sequence < 1:
            raise ValueError(
                f"sequence must be >= 1 (strictly monotonic); got {self.sequence}"
            )
        # Validate temporal ordering
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError(
                f"valid_until ({self.valid_until}) must be > valid_from "
                f"({self.valid_from})"
            )
        # Compute or verify fact_id
        computed = compute_fact_id(
            self.predicate, self.subject, self.object, self.valid_from,
        )
        if not self.fact_id:
            # Fresh construction: assign computed hash
            self.fact_id = computed
        elif self.fact_id != computed:
            raise ValueError(
                f"fact_id mismatch on construction: provided "
                f"{self.fact_id.hex()}, computed {computed.hex()}. "
                f"Did one of (predicate, subject, object, valid_from) change?"
            )
        # Validate digest length
        if len(self.fact_id) != FACT_ID_BYTES:
            raise ValueError(
                f"fact_id must be {FACT_ID_BYTES} bytes; got {len(self.fact_id)}"
            )


@dataclass(slots=True)
class Turn:
    """A single conversational turn. See 01-workload-spec.md §3.4.

    Effects applied in fixed order per Rule O1 (§3.7):
        1. read   (`requires`)
        2. apply  (`introduces`)
        3. apply  (`deletes`)

    `as_of` (v0.1.2) is the valid-time of a query: it tells the backend to
    answer as the world stood at that instant. Only agent_query turns may
    set it. None means "current state" (latest-wins default).
    """
    turn_id: UUID
    role: TurnRole
    content: str
    content_template: str
    timestamp: datetime
    introduces: list[bytes] = field(default_factory=list)
    deletes: list[bytes] = field(default_factory=list)
    requires: list[bytes] = field(default_factory=list)
    expected_response: str | None = None
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("Turn.timestamp must be timezone-aware")
        # Rule V1: introduces and deletes must be disjoint within one turn
        intro_set = set(self.introduces)
        del_set = set(self.deletes)
        overlap = intro_set & del_set
        if overlap:
            raise ValueError(
                f"V1 violation: turn introduces and deletes the same fact(s): "
                f"{[fid.hex() for fid in overlap]}"
            )
        # requires is only meaningful for agent_query turns
        if self.requires and self.role != TurnRole.AGENT_QUERY:
            raise ValueError(
                f"Turn.requires set on non-query role {self.role!r}; "
                f"only agent_query turns can have requirements"
            )
        # as_of is the valid-time of a query; only agent_query turns carry it
        if self.as_of is not None:
            if self.role != TurnRole.AGENT_QUERY:
                raise ValueError(
                    f"Turn.as_of set on non-query role {self.role!r}; "
                    f"only agent_query turns can specify a valid-time"
                )
            if self.as_of.tzinfo is None:
                raise ValueError("Turn.as_of must be timezone-aware")


@dataclass(slots=True)
class Session:
    """A conversational session. See 01-workload-spec.md §3.3."""
    session_id: UUID
    start_time: datetime
    end_time: datetime
    turns: list[Turn] = field(default_factory=list)
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        if self.end_time < self.start_time:
            raise ValueError(
                f"Session.end_time ({self.end_time}) < start_time "
                f"({self.start_time})"
            )


@dataclass(slots=True)
class GroundTruth:
    """Derived oracle for a Trace. See 01-workload-spec.md §3.5.

    All fields are typically computed by the oracle (see oracle.py), not
    authored directly. This dataclass exists so the data type round-trips
    cleanly through JSON.
    """
    recall_targets: dict[UUID, set[bytes]] = field(default_factory=dict)
    active_memory: dict[UUID, set[bytes]] = field(default_factory=dict)
    superseded_chains: dict[bytes, list[bytes]] = field(default_factory=dict)
    tenant_partitions: dict[str, set[bytes]] = field(default_factory=dict)
    deleted_facts: dict[bytes, datetime] = field(default_factory=dict)


@dataclass(slots=True)
class Trace:
    """Top-level trace unit. See 01-workload-spec.md §3.1.

    `facts` is the FINAL state of the world after all turn effects
    (post-supersession, post-deletion). Intermediate states are
    reconstructible from `sessions` + `ground_truth`.
    """
    trace_id: UUID
    workload: Workload
    difficulty: Difficulty
    seed: int
    facts: list[Fact] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)
    ground_truth: GroundTruth = field(default_factory=GroundTruth)
    paraphrase_meta: ParaphraseMeta | None = None
    schema_version: str = SCHEMA_VERSION
    generator_version: str = GENERATOR_VERSION
    generated_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )


# ============================================================================
# Serialization
# ============================================================================

def fact_to_dict(f: Fact) -> dict[str, Any]:
    """Serialize a Fact to a JSON-compatible dict."""
    return {
        "fact_id": f.fact_id.hex(),
        "sequence": f.sequence,
        "predicate": f.predicate,
        "subject": f.subject,
        "object": f.object,
        "valid_from": _format_iso_microsecond(f.valid_from),
        "valid_until": (
            _format_iso_microsecond(f.valid_until)
            if f.valid_until is not None else None
        ),
        "superseded_by": f.superseded_by.hex() if f.superseded_by else None,
        "importance": f.importance,
        "tenant_id": f.tenant_id,
        "source_turn_id": (
            str(f.source_turn_id) if f.source_turn_id is not None else None
        ),
    }


def fact_from_dict(d: dict[str, Any]) -> Fact:
    """Deserialize a Fact from a dict, verifying fact_id integrity."""
    return Fact(
        fact_id=bytes.fromhex(d["fact_id"]),
        sequence=d["sequence"],
        predicate=d["predicate"],
        subject=d["subject"],
        object=d["object"],
        valid_from=_parse_iso_microsecond(d["valid_from"]),
        valid_until=(
            _parse_iso_microsecond(d["valid_until"])
            if d.get("valid_until") is not None else None
        ),
        superseded_by=(
            bytes.fromhex(d["superseded_by"])
            if d.get("superseded_by") else None
        ),
        importance=d.get("importance", 1.0),
        tenant_id=d.get("tenant_id"),
        source_turn_id=(
            UUID(d["source_turn_id"])
            if d.get("source_turn_id") is not None else None
        ),
    )


def turn_to_dict(t: Turn) -> dict[str, Any]:
    return {
        "turn_id": str(t.turn_id),
        "role": t.role.value,
        "content": t.content,
        "content_template": t.content_template,
        "timestamp": _format_iso_microsecond(t.timestamp),
        "introduces": [fid.hex() for fid in t.introduces],
        "deletes": [fid.hex() for fid in t.deletes],
        "requires": [fid.hex() for fid in t.requires],
        "expected_response": t.expected_response,
        "as_of": (
            _format_iso_microsecond(t.as_of) if t.as_of is not None else None
        ),
    }


def turn_from_dict(d: dict[str, Any]) -> Turn:
    return Turn(
        turn_id=UUID(d["turn_id"]),
        role=TurnRole(d["role"]),
        content=d["content"],
        content_template=d["content_template"],
        timestamp=_parse_iso_microsecond(d["timestamp"]),
        introduces=[bytes.fromhex(h) for h in d.get("introduces", [])],
        deletes=[bytes.fromhex(h) for h in d.get("deletes", [])],
        requires=[bytes.fromhex(h) for h in d.get("requires", [])],
        expected_response=d.get("expected_response"),
        as_of=(
            _parse_iso_microsecond(d["as_of"])
            if d.get("as_of") is not None else None
        ),
    )


def session_to_dict(s: Session) -> dict[str, Any]:
    return {
        "session_id": str(s.session_id),
        "tenant_id": s.tenant_id,
        "start_time": _format_iso_microsecond(s.start_time),
        "end_time": _format_iso_microsecond(s.end_time),
        "turns": [turn_to_dict(t) for t in s.turns],
    }


def session_from_dict(d: dict[str, Any]) -> Session:
    return Session(
        session_id=UUID(d["session_id"]),
        tenant_id=d.get("tenant_id"),
        start_time=_parse_iso_microsecond(d["start_time"]),
        end_time=_parse_iso_microsecond(d["end_time"]),
        turns=[turn_from_dict(t) for t in d.get("turns", [])],
    )


def ground_truth_to_dict(gt: GroundTruth) -> dict[str, Any]:
    """Serialize GroundTruth. Sets are emitted as sorted lists for
    deterministic on-disk representation."""
    def sorted_hex_list(s: set[bytes]) -> list[str]:
        return sorted(b.hex() for b in s)

    return {
        "recall_targets": {
            str(tid): sorted_hex_list(facts)
            for tid, facts in gt.recall_targets.items()
        },
        "active_memory": {
            str(tid): sorted_hex_list(facts)
            for tid, facts in gt.active_memory.items()
        },
        "superseded_chains": {
            head.hex(): [f.hex() for f in chain]
            for head, chain in gt.superseded_chains.items()
        },
        "tenant_partitions": {
            tenant: sorted_hex_list(facts)
            for tenant, facts in gt.tenant_partitions.items()
        },
        "deleted_facts": {
            fid.hex(): _format_iso_microsecond(t)
            for fid, t in gt.deleted_facts.items()
        },
    }


def ground_truth_from_dict(d: dict[str, Any]) -> GroundTruth:
    return GroundTruth(
        recall_targets={
            UUID(tid): {bytes.fromhex(h) for h in hexes}
            for tid, hexes in d.get("recall_targets", {}).items()
        },
        active_memory={
            UUID(tid): {bytes.fromhex(h) for h in hexes}
            for tid, hexes in d.get("active_memory", {}).items()
        },
        superseded_chains={
            bytes.fromhex(head): [bytes.fromhex(f) for f in chain]
            for head, chain in d.get("superseded_chains", {}).items()
        },
        tenant_partitions={
            tenant: {bytes.fromhex(h) for h in hexes}
            for tenant, hexes in d.get("tenant_partitions", {}).items()
        },
        deleted_facts={
            bytes.fromhex(fid): _parse_iso_microsecond(t)
            for fid, t in d.get("deleted_facts", {}).items()
        },
    )


def trace_to_dict(trace: Trace) -> dict[str, Any]:
    """Serialize a Trace to a JSON-compatible dict."""
    return {
        "schema_version": trace.schema_version,
        "trace_id": str(trace.trace_id),
        "workload": trace.workload.value,
        "difficulty": trace.difficulty.value,
        "seed": trace.seed,
        "generator_version": trace.generator_version,
        "generated_at": _format_iso_microsecond(trace.generated_at),
        "facts": [fact_to_dict(f) for f in trace.facts],
        "sessions": [session_to_dict(s) for s in trace.sessions],
        "ground_truth": ground_truth_to_dict(trace.ground_truth),
        "paraphrase_meta": (
            {
                "model_id": trace.paraphrase_meta.model_id,
                "prompt_version": trace.paraphrase_meta.prompt_version,
                "temperature": trace.paraphrase_meta.temperature,
                "cache_key_scheme": trace.paraphrase_meta.cache_key_scheme,
            } if trace.paraphrase_meta else None
        ),
    }


def trace_from_dict(d: dict[str, Any]) -> Trace:
    """Deserialize a Trace from a dict. Verifies fact_id integrity."""
    pm_dict = d.get("paraphrase_meta")
    paraphrase = ParaphraseMeta(**pm_dict) if pm_dict else None

    return Trace(
        schema_version=d["schema_version"],
        trace_id=UUID(d["trace_id"]),
        workload=Workload(d["workload"]),
        difficulty=Difficulty(d["difficulty"]),
        seed=d["seed"],
        generator_version=d["generator_version"],
        generated_at=_parse_iso_microsecond(d["generated_at"]),
        facts=[fact_from_dict(f) for f in d.get("facts", [])],
        sessions=[session_from_dict(s) for s in d.get("sessions", [])],
        ground_truth=ground_truth_from_dict(d.get("ground_truth", {})),
        paraphrase_meta=paraphrase,
    )


# ============================================================================
# JSON Schema (the on-disk contract)
# ============================================================================

# Reusable subschemas
_HEX_FACT_ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}  # 16 bytes hex
_UUID_STR = {
    "type": "string",
    "pattern": (
        "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        "[0-9a-f]{4}-[0-9a-f]{12}$"
    ),
}
_ISO_MICROSECOND = {
    "type": "string",
    # Lenient pattern; full RFC3339 validation is too heavy here.
    "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}",
}

TRACE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://grafomem.org/schemas/trace-v0.1.1.json",
    "title": "GRAFOMEM Trace",
    "description": "Trace schema v0.1.1 per 01-workload-spec.md",
    "type": "object",
    "required": [
        "schema_version", "trace_id", "workload", "difficulty", "seed",
        "generator_version", "generated_at", "facts", "sessions",
        "ground_truth",
    ],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "trace_id": _UUID_STR,
        "workload": {"enum": [w.value for w in Workload]},
        "difficulty": {"enum": [d.value for d in Difficulty]},
        "seed": {"type": "integer", "minimum": 0},
        "generator_version": {"type": "string"},
        "generated_at": _ISO_MICROSECOND,
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "fact_id", "sequence", "predicate", "subject",
                    "object", "valid_from", "importance",
                ],
                "properties": {
                    "fact_id": _HEX_FACT_ID,
                    "sequence": {"type": "integer", "minimum": 1},
                    "predicate": {"enum": sorted(CONTROLLED_PREDICATES)},
                    "subject": {"type": "string", "minLength": 1},
                    "object": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "number"},
                            {"type": "boolean"},
                        ],
                    },
                    "valid_from": _ISO_MICROSECOND,
                    "valid_until": {
                        "oneOf": [_ISO_MICROSECOND, {"type": "null"}],
                    },
                    "superseded_by": {
                        "oneOf": [_HEX_FACT_ID, {"type": "null"}],
                    },
                    "importance": {
                        "type": "number", "minimum": 0.0, "maximum": 1.0,
                    },
                    "tenant_id": {
                        "oneOf": [{"type": "string"}, {"type": "null"}],
                    },
                    "source_turn_id": {
                        "oneOf": [_UUID_STR, {"type": "null"}],
                    },
                },
            },
        },
        "sessions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["session_id", "start_time", "end_time", "turns"],
                "properties": {
                    "session_id": _UUID_STR,
                    "tenant_id": {
                        "oneOf": [{"type": "string"}, {"type": "null"}],
                    },
                    "start_time": _ISO_MICROSECOND,
                    "end_time": _ISO_MICROSECOND,
                    "turns": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": [
                                "turn_id", "role", "content",
                                "content_template", "timestamp",
                            ],
                            "properties": {
                                "turn_id": _UUID_STR,
                                "role": {
                                    "enum": [r.value for r in TurnRole],
                                },
                                "content": {"type": "string"},
                                "content_template": {"type": "string"},
                                "timestamp": _ISO_MICROSECOND,
                                "introduces": {
                                    "type": "array", "items": _HEX_FACT_ID,
                                },
                                "deletes": {
                                    "type": "array", "items": _HEX_FACT_ID,
                                },
                                "requires": {
                                    "type": "array", "items": _HEX_FACT_ID,
                                },
                                "expected_response": {
                                    "oneOf": [
                                        {"type": "string"}, {"type": "null"},
                                    ],
                                },
                                "as_of": {
                                    "oneOf": [
                                        _ISO_MICROSECOND, {"type": "null"},
                                    ],
                                },
                            },
                        },
                    },
                },
            },
        },
        "ground_truth": {
            "type": "object",
            "properties": {
                "recall_targets": {"type": "object"},
                "active_memory": {"type": "object"},
                "superseded_chains": {"type": "object"},
                "tenant_partitions": {"type": "object"},
                "deleted_facts": {"type": "object"},
            },
        },
        "paraphrase_meta": {
            "oneOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "required": [
                        "model_id", "prompt_version", "temperature",
                        "cache_key_scheme",
                    ],
                    "properties": {
                        "model_id": {"type": "string"},
                        "prompt_version": {"type": "string"},
                        "temperature": {"type": "number"},
                        "cache_key_scheme": {"type": "string"},
                    },
                },
            ],
        },
    },
}


def validate_trace_schema(trace_dict: dict[str, Any]) -> None:
    """Validate a serialized Trace against the JSON Schema.

    Raises jsonschema.ValidationError on failure. This is structural
    validation only; semantic validation (V1-V5) lives in validators.py.
    """
    if jsonschema is None:
        raise RuntimeError(
            "jsonschema library not installed; "
            "run `pip install jsonschema>=4.0` to enable validation"
        )
    jsonschema.validate(trace_dict, TRACE_SCHEMA)


# ============================================================================
# Smoke check — run `python trace.py` for immediate diagnostic feedback
# ============================================================================

if __name__ == "__main__":
    print(f"GRAFOMEM trace.py — schema v{SCHEMA_VERSION}, "
          f"generator v{GENERATOR_VERSION}\n")

    # --- Test 1: content-addressing determinism ----------------------------
    t0 = datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)
    fid_a = compute_fact_id("lives_in", "user_42", "Rome", t0)
    fid_b = compute_fact_id("lives_in", "user_42", "Rome", t0)
    assert fid_a == fid_b, "deterministic hashing failed"
    print(f"✓ Content-addressing deterministic   "
          f"(fact_id = {fid_a.hex()[:16]}...)")

    # --- Test 2: type sensitivity (1 vs 1.0 vs True) ----------------------
    fid_int = compute_fact_id("costs", "coffee", 1, t0)
    fid_float = compute_fact_id("costs", "coffee", 1.0, t0)
    fid_bool = compute_fact_id("costs", "coffee", True, t0)
    assert fid_int != fid_float != fid_bool, \
        "type-sensitive hashing failed"
    print(f"✓ Type-sensitive hashing             "
          f"(int/float/bool produce distinct ids)")

    # --- Test 3: Fact construction with auto-computed fact_id -------------
    f1 = Fact(
        predicate="lives_in",
        subject="user_42",
        object="Rome",
        valid_from=t0,
        sequence=1,
        importance=0.9,
    )
    assert f1.fact_id == fid_a
    print(f"✓ Fact auto-computes fact_id         "
          f"(matches manual compute)")

    # --- Test 4: Fact rejects fact_id tampering ---------------------------
    try:
        Fact(
            predicate="lives_in",
            subject="user_42",
            object="Rome",
            valid_from=t0,
            sequence=1,
            fact_id=b"\x00" * 16,  # wrong hash
        )
        raise AssertionError("expected ValueError on fact_id mismatch")
    except ValueError as e:
        assert "fact_id mismatch" in str(e)
    print(f"✓ Fact rejects fact_id tampering     "
          f"(integrity verified on construction)")

    # --- Test 5: Turn enforces Rule V1 (no introduce+delete same fact) ---
    try:
        Turn(
            turn_id=uuid4(),
            role=TurnRole.USER,
            content="oops",
            content_template="oops",
            timestamp=t0,
            introduces=[fid_a],
            deletes=[fid_a],
        )
        raise AssertionError("expected ValueError on V1 violation")
    except ValueError as e:
        assert "V1 violation" in str(e)
    print(f"✓ Turn enforces Rule V1              "
          f"(introduces ∩ deletes = ∅)")

    # --- Test 6: Full Trace round-trip ------------------------------------
    turn = Turn(
        turn_id=uuid4(),
        role=TurnRole.USER,
        content="I live in Rome.",
        content_template="I live in Rome.",
        timestamp=t0,
        introduces=[f1.fact_id],
    )
    session = Session(
        session_id=uuid4(),
        start_time=t0,
        end_time=t0.replace(microsecond=t0.microsecond + 1),
        turns=[turn],
    )
    trace = Trace(
        trace_id=uuid4(),
        workload=Workload.W1,
        difficulty=Difficulty.EASY,
        seed=0,
        facts=[f1],
        sessions=[session],
    )

    d = trace_to_dict(trace)
    json_str = json.dumps(d, indent=2)
    restored = trace_from_dict(json.loads(json_str))

    assert restored.facts[0].fact_id == f1.fact_id
    assert restored.sessions[0].turns[0].turn_id == turn.turn_id
    assert restored.trace_id == trace.trace_id
    print(f"✓ Round-trip serialization clean     "
          f"(JSON size: {len(json_str)} bytes)")

    # --- Test 7: JSON-Schema validation -----------------------------------
    if jsonschema is not None:
        validate_trace_schema(d)
        print(f"✓ JSON-Schema validation passed      "
              f"(against TRACE_SCHEMA v{SCHEMA_VERSION})")
    else:
        print(f"⚠ JSON-Schema validation skipped     "
              f"(jsonschema not installed)")

    # --- Test 8: as_of bi-temporal query field (v0.1.2) -------------------
    q_turn = Turn(
        turn_id=uuid4(),
        role=TurnRole.AGENT_QUERY,
        content="Where did I used to live?",
        content_template="Where did I used to live?",
        timestamp=t0,
        requires=[f1.fact_id],
        as_of=t0,
    )
    q_restored = turn_from_dict(turn_to_dict(q_turn))
    assert q_restored.as_of == t0, "as_of round-trip failed"
    print(f"✓ Query as_of round-trips            "
          f"(v0.1.2 bi-temporal query field)")

    try:
        Turn(
            turn_id=uuid4(),
            role=TurnRole.USER,
            content="x",
            content_template="x",
            timestamp=t0,
            as_of=t0,  # illegal: as_of on a non-query turn
        )
        raise AssertionError("expected ValueError on as_of for non-query turn")
    except ValueError as e:
        assert "as_of" in str(e)
    print(f"✓ as_of rejected on non-query turns  "
          f"(only agent_query carries valid-time)")

    print(f"\nAll smoke checks green. Ready for oracle.py.")
