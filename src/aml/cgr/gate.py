"""CGR Gate-1 (Ticket B2b) — the review-channel calibration gate.

PROPRIETARY. The soft-ramp g(w), the τ threshold, the per-source cap K, and the
per-tenant `agent_calibration` lookup live HERE, behind the scoring.py injection seam
(`WeightingConfig.review_gate` / `review_cap_k`), so `aml/cgr/scoring.py` stays a
generic Beta scorer with no gate literals in it.

Gate-1 hardens the SYBIL surface — review farms and uncalibrated cold-start sources —
by weighting each review SOURCE by its proven calibration `w`. The verifiable channel
(real resolved certify/judgment outcomes) is NEVER gated; that surface is
competence-fraud (B2a), not Sybil.

Boundary / moat: this logic lives ONLY in grafomem CGR, never in the meridian sim.
The sim's local `Scorer` stub does not have it, so a farmed thin target that the local
stub inflates is FLOORED by grafomem — the divergence is the moat
(tests/test_gate1_cold_start.py::test_divergence_moat).

Write authority (design): `agent_calibration.calibration_weight` is writable ONLY by
the identity authority (sim operator / GEIANT) holding the privileged `calibration:write`
scope — NEVER by an agent's own ingestion key. A self-assignable `w` defeats the gate.
Populating `w` is gated on that write-path enforcement being reviewed and in place.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Mapping


@contextmanager
def calibration_tenant_tx(pool, tenant_id: str):
    """Transaction-local tenant scope for the RLS-FORCEd calibration tables
    (`agent_calibration`, `cgr_gate_config`), plus the same-tenant audit write that
    joins the scope (`gcrumbs_breadcrumbs`).

    Opens ``conn.transaction()`` FIRST, then sets ``app.current_tenant`` with
    ``is_local=True`` so Postgres auto-resets the GUC at COMMIT/ROLLBACK — no
    session-scoped setter, no ``finally``-reset, no risk of the tenant GUC leaking to
    the next borrower of a pooled connection. Yields the connection so a single scope
    can cover a read (config + calibration) OR an atomic write (calibration upsert +
    audit breadcrumb).

    Canonical pattern: ``aml.cloud.world_model.WorldModelService._tenant_tx``
    (grafomem PR #46). TODO(cleanup): unify these two into one shared db helper —
    deferred; do NOT re-touch the shipped WorldModelService in this change.
    """
    conn = pool.getconn()
    try:
        with conn.transaction():                                   # opens FIRST
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.current_tenant', %s, true)", (tenant_id,))
            yield conn
    finally:
        pool.putconn(conn)


def review_gate_g(w: float | None, tau: float) -> float:
    """Soft-ramp calibration gate  g(w) = max(0, (w − τ) / (1 − τ)), clamped to [0, 1].

    * Unknown / absent `w` ⇒ 0 — the cold-start fail-safe: an unproven source
      contributes nothing until it earns a calibration weight.
    * A source at or below τ contributes nothing; contribution ramps linearly to 1 at w=1.
    """
    if w is None:
        return 0.0
    if not (0.0 <= tau < 1.0):
        return 0.0
    val = (float(w) - tau) / (1.0 - tau)
    if val <= 0.0:
        return 0.0
    return 1.0 if val > 1.0 else val


def build_review_gate(calibration: Mapping[str, float | None], tau: float) -> Callable[[str], float]:
    """Return a `source_id → g(w)` callable for `WeightingConfig.review_gate`, backed by
    the per-tenant calibration map (`source_id → w`). Sources absent from the map ⇒ g=0
    (fail-safe). The engine builds `calibration` from the tenant's `agent_calibration`
    rows (RLS-scoped) resolving each review source to its GEIANT `agent_key`."""
    def _gate(source_id: str) -> float:
        return review_gate_g(calibration.get(source_id), tau)
    return _gate


def newcomer_exclusion_pct(calibration: Mapping[str, float | None], sources, tau: float) -> float:
    """Reported metric (§6.5): the fraction of `sources` fully excluded at τ (g(w)=0),
    i.e. unknown or w ≤ τ. Surfaced by the offline gate to size the cold-start floor."""
    srcs = list(sources)
    if not srcs:
        return 0.0
    excluded = sum(1 for s in srcs if review_gate_g(calibration.get(s), tau) == 0.0)
    return excluded / len(srcs)


# ── engine resolution: per-tenant config + calibration → gate (or NEUTRAL) ──────
@dataclass(frozen=True)
class GateConfig:
    """Per-tenant Gate-1 operating point (τ, K). Loaded from cgr_gate_config."""
    tau: float
    cap_k: float


def _cell(row, name, idx):
    return row[name] if isinstance(row, dict) else row[idx]


def load_gate_config(conn, tenant_id: str) -> "GateConfig | None":
    """Read the per-tenant Gate-1 config. None ⇒ gate OFF. Missing table (not yet
    migrated), no row, a disabled row, or ANY error ⇒ None (fail-safe to neutral so
    prod is byte-identical until config exists)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tau, cap_k, enabled FROM cgr_gate_config WHERE tenant_id = %s",
                (tenant_id,))
            row = cur.fetchone()
    except Exception:
        return None
    if not row or not _cell(row, "enabled", 2):
        return None
    tau, cap_k = _cell(row, "tau", 0), _cell(row, "cap_k", 1)
    if tau is None or cap_k is None:
        return None
    return GateConfig(tau=float(tau), cap_k=float(cap_k))


def load_calibration(conn, tenant_id: str) -> dict[str, float]:
    """Read {agent_key: calibration_weight} for the tenant (RLS-scoped). Empty on any error."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT agent_key, calibration_weight FROM agent_calibration "
                "WHERE tenant_id = %s AND calibration_weight IS NOT NULL",
                (tenant_id,))
            rows = cur.fetchall()
    except Exception:
        return {}
    out: dict[str, float] = {}
    for r in rows:
        k, w = _cell(r, "agent_key", 0), _cell(r, "calibration_weight", 1)
        if k is not None and w is not None:
            out[str(k)] = float(w)
    return out


def resolve_review_gate(conn, tenant_id: str):
    """Return (review_gate callable | None, cap_k | None) for WeightingConfig.

    (None, None) ⇒ NEUTRAL — no config, OR config present but zero calibration rows.
    The gate turns on ONLY when a tenant has BOTH an enabled config AND ≥1 calibration
    weight, so every other tenant (incl. corp) scores byte-identically to v1."""
    cfg = load_gate_config(conn, tenant_id)
    if cfg is None:
        return None, None
    calibration = load_calibration(conn, tenant_id)
    if not calibration:
        return None, None
    return build_review_gate(calibration, cfg.tau), cfg.cap_k
