#!/usr/bin/env python3
"""§5.3 continues-edge ceremony — ONE-SHOT ADMIN SCRIPT. Not wired to any route.

Issues ONE Foundation-side `continues` lineage record: "successor B continues predecessor A"
(A anchored by its delegation `cert_hash`, §5.3.3). The record is written append-only to the
identity store; the read surface injects it into B's re-minted v4 attestation (Option A), so a
verifier reaching B can navigate to A's orphaned chain. `continues` is navigable lineage ONLY — it
has NO scoring consequence (spec §5.3.4).

This is the operator's ceremony, run out-of-band, exactly like a key rotation — NOT an API route and
NOT invoked by the server. It REFUSES to write unless ALL FOUR §5.3 preconditions hold:

  1. Control of B      — the operator signs a challenge nonce with B's secret key (verified here).
  2. A genuinely retired — A is revoked in geiant's `agent_registry` (revoked_at IS NOT NULL).
  3. Anti-fork unique  — no existing continues edge already targets A's cert_hash (§5.3.4).
  4. Honest tier       — the evidence tier is recorded on the edge (§1.1); for the first case
                         (c14094ea → d3caa6f1) that is `operator_verification` (custody unavailable
                         until 0005 lands; issuer_records would overstate it — §5.3.2).

Any failing precondition ⇒ REFUSE, print the reason, exit non-zero, write nothing.

Usage (the operator runs this; the repo does not):

    GRAFOMEM_DB_URL=... GEIANT_DB_URL=... python scripts/cgr_continues_ceremony.py \
        --tenant <tenant_id> \
        --b-key <B agent_pk hex> --nonce <challenge> --b-sig <B's sig over the nonce, hex> \
        --a-agent-pk <A agent_pk hex> --a-cert-hash <A cert_hash sha-256 hex> \
        --decision-date 2026-09-04 [--evidence-tier operator_verification] [--dry-run]

`--dry-run` runs all four checks and reports, but writes nothing.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

# Closed evidence-tier vocabulary (§1.1), 1:1 with the §5.3.2 authority tiers.
_EVIDENCE_TIERS = ("custody_record", "issuer_records", "operator_verification")


# ── preconditions (each returns (ok: bool, reason: str)) ─────────────────────────────────

def check_control_of_b(b_pubkey_hex: str, nonce: str, b_sig_hex: str) -> tuple[bool, str]:
    """(1) Control of B: the operator signed the challenge nonce with B's secret key. B is not
    compromised, so a signature from B is meaningful (unlike A — §5.2)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(b_pubkey_hex))
        pk.verify(bytes.fromhex(b_sig_hex), nonce.encode("utf-8"))
        return True, "B signed the challenge nonce (control of B proven)"
    except Exception as e:  # noqa: BLE001 — any failure is a refusal
        return False, f"control-of-B FAILED: B's signature over the nonce did not verify ({e})"


def check_a_revoked(geiant_conn, a_agent_pk: str) -> tuple[bool, str]:
    """(2) A genuinely retired: A is revoked in geiant's enforcement index (agent_registry.revoked_at).
    A `continues` into a LIVE A would be a fork, not a rotation (§5.3.3)."""
    with geiant_conn.cursor() as cur:
        cur.execute("SELECT revoked_at FROM agent_registry WHERE agent_pk = %s", (a_agent_pk,))
        row = cur.fetchone()
    if row is None:
        return False, f"A-revoked FAILED: {a_agent_pk[:12]}… not found in geiant agent_registry"
    if row[0] is None:
        return False, ("A-revoked FAILED: A.revoked_at IS NULL — A is still live; a continues into a "
                       "live A is a fork, not a rotation")
    return True, f"A is revoked (revoked_at = {row[0]})"


def check_anti_fork(store_manager, tenant_id: str, a_cert_hash: str) -> tuple[bool, str]:
    """(3) Anti-fork uniqueness: no existing continues edge already targets A's cert_hash. Without this
    two parties could each claim to continue A and split its history (§5.3.4). Foundation-side ledger
    check at issuance — the schema can't see other edges (§3 asymmetry)."""
    from aml.cgr.substrate import load_continues_edges
    for subject, edge in load_continues_edges(store_manager, tenant_id).items():
        if edge.get("target", {}).get("hash") == a_cert_hash:
            return False, (f"anti-fork FAILED: {subject[:12]}… already continues A cert "
                           f"{a_cert_hash[:12]}… — at most one successor per predecessor")
    return True, "anti-fork OK: no existing continues edge targets A"


def check_tier(evidence_tier: str) -> tuple[bool, str]:
    """(4) Honest tier: the recorded evidence_tier is in the closed §1.1 vocabulary."""
    if evidence_tier not in _EVIDENCE_TIERS:
        return False, f"tier FAILED: evidence_tier {evidence_tier!r} not in {_EVIDENCE_TIERS}"
    return True, f"evidence_tier recorded as {evidence_tier!r}"


# ── the ceremony ─────────────────────────────────────────────────────────────────────────

def run_ceremony(*, store_manager, geiant_conn, tenant_id: str, b_key: str, nonce: str, b_sig: str,
                 a_agent_pk: str, a_cert_hash: str, decision_date: str,
                 evidence_tier: str = "operator_verification", dry_run: bool = False) -> int:
    """Run all four §5.3 preconditions; write ONE continues-edge record iff all pass. Returns a
    process exit code (0 = written / dry-run-clean; non-zero = refused)."""
    checks = [
        check_control_of_b(b_key, nonce, b_sig),
        check_a_revoked(geiant_conn, a_agent_pk),
        check_anti_fork(store_manager, tenant_id, a_cert_hash),
        check_tier(evidence_tier),
    ]
    print("§5.3 continues ceremony — preconditions:")
    for ok, reason in checks:
        print(f"  [{'PASS' if ok else 'REFUSE'}] {reason}")
    if not all(ok for ok, _ in checks):
        print("\nREFUSED — one or more §5.3 preconditions failed. Nothing written.", file=sys.stderr)
        return 2
    if dry_run:
        print("\n--dry-run: all preconditions pass; NOT writing.")
        return 0

    # All four hold → write the single append-only edge record.
    from aml.backends.interface import WriteOptions
    from aml.cgr.substrate import CGR_ROTATION_STORE, continues_edge_metadata
    recorded_at = datetime.now(timezone.utc).isoformat()
    meta = continues_edge_metadata(
        subject_key=b_key, target_hash=a_cert_hash, evidence_tier=evidence_tier,
        decision_date=decision_date, recorded_at=recorded_at,
        ceremony_ref=f"cgr-continues-ceremony/{tenant_id[:12]}/{recorded_at}",
    )
    backend = store_manager.get_or_create_named(CGR_ROTATION_STORE).backend
    content = f"continues | {b_key} -> cert {a_cert_hash} | tier={evidence_tier}"
    backend.write(content, WriteOptions(valid_from=datetime.now(timezone.utc), tenant_id=tenant_id,
                                        metadata=meta, skip_embedding=True))
    print(f"\nWROTE continues edge: {b_key[:12]}… continues A cert {a_cert_hash[:12]}… "
          f"(tier={evidence_tier}). It will surface in B's next re-minted v4 attestation.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="§5.3 continues-edge ceremony (one-shot admin)")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--b-key", required=True, help="successor B agent_pk (hex)")
    ap.add_argument("--nonce", required=True, help="challenge nonce B signed")
    ap.add_argument("--b-sig", required=True, help="B's Ed25519 signature over the nonce (hex)")
    ap.add_argument("--a-agent-pk", required=True, help="predecessor A agent_pk (hex)")
    ap.add_argument("--a-cert-hash", required=True, help="A delegation cert_hash (sha-256 hex)")
    ap.add_argument("--decision-date", required=True, help="date the continuity was determined")
    ap.add_argument("--evidence-tier", default="operator_verification", choices=_EVIDENCE_TIERS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    grafomem_db = os.environ.get("GRAFOMEM_DB_URL")
    geiant_db = os.environ.get("GEIANT_DB_URL")
    if not grafomem_db or not geiant_db:
        print("set GRAFOMEM_DB_URL (store) and GEIANT_DB_URL (agent_registry) in the env",
              file=sys.stderr)
        return 3

    import psycopg
    from aml.backends.postgres_gmp import PostgresGMPBackend
    from aml.server.stores import StoreManager

    sm = StoreManager(lambda: PostgresGMPBackend(grafomem_db))
    with psycopg.connect(geiant_db) as geiant_conn:
        return run_ceremony(
            store_manager=sm, geiant_conn=geiant_conn, tenant_id=args.tenant,
            b_key=args.b_key, nonce=args.nonce, b_sig=args.b_sig,
            a_agent_pk=args.a_agent_pk, a_cert_hash=args.a_cert_hash,
            decision_date=args.decision_date, evidence_tier=args.evidence_tier,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":  # pragma: no cover — operator entry point; never imported by the server
    sys.exit(main())
