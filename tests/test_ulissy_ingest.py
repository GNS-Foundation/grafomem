"""Phase-0 Ulissy ingestion — unit coverage for the new adapter, plus a focused
cross-tenant no-leak assertion.

The mapping/idempotency tests are pure (no DB, no HTTP) and always run. The isolation
test reuses the shipped scoped_audit path (the #12a guarantee) with an Ulissy-vs-demo
tenant pair, and SKIPS cleanly when a local Postgres isn't reachable — it must never
silently pass. The deep RLS proof lives in tests/test_cgr_rls.py; this asserts the
property holds for the *new tenant* scenario.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

# Make ops/ importable and pull in the HTTP-free mapping layer.
_OPS = Path(__file__).resolve().parent.parent / "ops"
sys.path.insert(0, str(_OPS))
import ingest_front as ing  # noqa: E402

AGENT = "gtm-outreach-agent@ulissy"
KEY = "a" * 64  # stable synthetic agent_key


# ---------------------------------------------------------------------------
# make_ref / slugify
# ---------------------------------------------------------------------------

def test_slugify_stable_and_safe():
    assert ing.slugify("Fasanara Capital!") == "fasanara-capital"
    assert ing.slugify("  Ana   Ruiz  ") == "ana-ruiz"
    assert ing.slugify("A/B & C") == "a-b-c"


def test_make_ref_explicit_wins():
    assert ing.make_ref({"ref": "OUT-x", "company": "Y", "person": "Z"}) == "OUT-x"


def test_make_ref_derived_from_company_person():
    assert ing.make_ref({"company": "Fasanara Capital", "person": "Ana Ruiz"}) == \
        "OUT-fasanara-capital-ana-ruiz"


def test_make_ref_requires_something():
    with pytest.raises(ValueError):
        ing.make_ref({"channel": "email"})


# ---------------------------------------------------------------------------
# row_to_decision — semantic mapping (A)
# ---------------------------------------------------------------------------

def test_row_to_decision_shape_and_edge_flags():
    row = {"company": "Acme", "person": "Luca Conti", "channel": "email",
           "message_variant": "v3", "rationale": "referral", "edge_approved": "no"}
    d = ing.row_to_decision(row, AGENT, KEY)
    assert d["decision"] == "certify"                 # every recorded outreach certifies
    assert d["invoice_id"] == "OUT-acme-luca-conti"   # ref -> invoice_id
    assert d["agent_handle"] == AGENT and d["agent_key"] == KEY
    assert d["verifiability_tag"] == "judgment"
    assert d["reason"] == "referral"
    ctx = d["context"]
    # The edge is load-bearing: recorded as an unexecuted proposal.
    assert ctx["edge_gate"] is True and ctx["executed"] is False
    assert ctx["edge_approved"] is False
    assert ctx["channel"] == "email" and ctx["company"] == "Acme"


def test_row_to_decision_tier_precedence():
    # explicit per-row tier wins over the default
    assert ing.row_to_decision({"ref": "r", "agent_tier": "0.7"}, AGENT, KEY, 0.2)["agent_tier"] == 0.7
    # falls back to the pinned default when the row has none
    assert ing.row_to_decision({"ref": "r"}, AGENT, KEY, 0.2)["agent_tier"] == 0.2
    # omitted entirely when neither is present
    assert "agent_tier" not in ing.row_to_decision({"ref": "r"}, AGENT, KEY)


# ---------------------------------------------------------------------------
# row_to_outcome — resolve mapping + interim discipline
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    ("meeting_booked", "paid"),
    ("Meeting Booked", "paid"),   # case/space-insensitive
    ("booked", "paid"),
    ("passed", "default"),
    ("no_response", "default"),
    ("no response", "default"),
    ("bounced", "default"),
])
def test_row_to_outcome_terminal(status, expected):
    o = ing.row_to_outcome({"ref": "r", "status": status})
    assert o is not None and o["outcome"] == expected
    assert o["invoice_ref"] == "r" and o["source"] == "gtm_ledger"


@pytest.mark.parametrize("status", ["", "proposed", "sent", "replied", "opened", "pending"])
def test_row_to_outcome_interim_emits_nothing(status):
    assert ing.row_to_outcome({"ref": "r", "status": status}) is None


def test_row_to_outcome_carries_date():
    o = ing.row_to_outcome({"ref": "r", "status": "meeting_booked", "resolved_date": "2026-07-28"})
    assert o["outcome_date"] == "2026-07-28"


def test_outcome_values_are_all_server_valid():
    # every mapped outcome must be in the governed _VALID_OUTCOMES set
    valid = {"paid", "default", "disputed", "late", "written_off"}
    assert set(ing.STATUS_TO_OUTCOME.values()) <= valid


# ---------------------------------------------------------------------------
# row_to_review
# ---------------------------------------------------------------------------

def test_row_to_review_none_without_reviewer_or_rating():
    assert ing.row_to_review({"ref": "r"}) is None
    assert ing.row_to_review({"ref": "r", "reviewer": "founder@ulissy"}) is None  # no rating
    assert ing.row_to_review({"ref": "r", "rating": "0.9"}) is None               # no reviewer


def test_row_to_review_clamps_rating():
    assert ing.row_to_review({"ref": "r", "reviewer": "f", "rating": "1.5"})["rating"] == 1.0
    assert ing.row_to_review({"ref": "r", "reviewer": "f", "rating": "-0.2"})["rating"] == 0.0


# ---------------------------------------------------------------------------
# unknown statuses — typo guard
# ---------------------------------------------------------------------------

def test_unknown_statuses_flagged():
    rows = [{"status": "meeting_booked"}, {"status": "ghosted"}, {"status": "replied"}]
    assert ing.unknown_statuses(rows) == ["ghosted"]   # not a known outcome or interim


# ---------------------------------------------------------------------------
# IngestState — decision idempotency (decisions have NO server dedup)
# ---------------------------------------------------------------------------

def test_ingest_state_roundtrip_and_dedup(tmp_path):
    p = str(tmp_path / "state.json")
    s = ing.IngestState(p)
    assert not s.has("OUT-x")
    s.add("OUT-x")
    s.save()
    # a fresh load sees the persisted ref -> re-runs skip it, preventing double-count
    s2 = ing.IngestState(p)
    assert s2.has("OUT-x") and not s2.has("OUT-y")


# ---------------------------------------------------------------------------
# sample fixture parses and maps as documented
# ---------------------------------------------------------------------------

def test_sample_ledger_maps_as_documented():
    rows = ing.load_rows(str(_OPS / "sample_ledger.csv"))
    assert len(rows) == 6
    outcomes = [o for r in rows if (o := ing.row_to_outcome(r))]
    kinds = sorted(o["outcome"] for o in outcomes)
    # two meeting_booked -> paid, one passed + one no_response -> default; replied/sent interim
    assert kinds == ["default", "default", "paid", "paid"]
    reviews = [rv for r in rows if (rv := ing.row_to_review(r))]
    assert len(reviews) == 3  # the three rows with reviewer+rating


# ---------------------------------------------------------------------------
# Cross-tenant no-leak — reuses the shipped scoped_audit path; SKIPS without PG
# ---------------------------------------------------------------------------

_TEST_DB_URL = "postgresql://grafomem:dev@localhost:5432/grafomem"


def _pg_backend():
    """A cgr-outcomes backend, or None if local Postgres isn't reachable."""
    try:
        import psycopg  # noqa: F401
        from aml.backends.postgres_gmp import PostgresGMPBackend
        from aml.server.stores import StoreManager
    except Exception:
        return None
    try:
        store = StoreManager(lambda: PostgresGMPBackend(_TEST_DB_URL))
        return store.get_or_create_named("cgr-outcomes").backend
    except Exception:
        return None


def test_ulissy_tenant_cannot_read_demo_rows():
    backend = _pg_backend()
    if backend is None:
        pytest.skip("local Postgres not reachable — cross-tenant proof must run on staging, "
                    "not silently pass. (Deep RLS proof: tests/test_cgr_rls.py.)")
    try:
        from aml.backends.interface import WriteOptions
        from aml.cgr.substrate import CGR_OUTCOME_SCHEMA, _scoped_audit

        ulissy = f"ulissy-{uuid.uuid4().hex[:8]}"
        demo = f"kapwork-demo-{uuid.uuid4().hex[:8]}"

        def _w(tenant, inv):
            meta = {"cgr_schema": CGR_OUTCOME_SCHEMA, "predicate": "receivable_outcome",
                    "subject": inv, "object": "paid"}
            backend.write(f"receivable_outcome | {inv} | paid", WriteOptions(tenant_id=tenant, metadata=meta))

        _w(ulissy, f"{ulissy}-OUT0")
        _w(demo, f"{demo}-SECRET")

        ulissy_rows = list(_scoped_audit(backend, ulissy))
        assert ulissy_rows, "Ulissy must see its own row"
        assert all(m.tenant_id == ulissy for m in ulissy_rows)                 # zero demo rows leak
        subjects = {(m.metadata or {}).get("subject") for m in ulissy_rows}
        assert f"{demo}-SECRET" not in subjects
    finally:
        backend.close()
