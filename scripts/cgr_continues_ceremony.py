#!/usr/bin/env python3
"""§5.3 continues-edge ceremony — ONE-SHOT ADMIN SCRIPT. Not wired to any route.

Issues ONE Foundation-side `continues` lineage record: "successor B continues predecessor A"
(A anchored by its delegation `cert_hash`, §5.3.3). The record is written append-only to the
identity store; the read surface injects it into B's re-minted v4 attestation (Option A), so a
verifier reaching B can navigate to A's orphaned chain. `continues` is navigable lineage ONLY — it
has NO scoring consequence (spec §5.3.4).

This is the operator's ceremony, run out-of-band, exactly like a key rotation — NOT an API route and
NOT invoked by the server. It REFUSES to write unless ALL FOUR §5.3 preconditions hold:

  1. Control of B      — the operator signs the CANONICAL CEREMONY MESSAGE (below) with B's secret
                         key. The message BINDS B's key + A's cert_hash, so a signature B made for any
                         other purpose cannot be replayed to pass this check.
  2. A genuinely retired — A is revoked in geiant's `agent_registry` (revoked_at IS NOT NULL), read
                         through the Supabase REST API with the service-role key — the SAME way every
                         geiant service reads the registry (geiant holds no raw-Postgres string for it).
  3. Anti-fork unique  — no existing continues edge already targets A's cert_hash (§5.3.4).
  4. Honest tier       — the evidence tier is recorded on the edge (§1.1); for the first case
                         (c14094ea → d3caa6f1) that is `operator_verification` (custody unavailable
                         until 0005 lands; issuer_records would overstate it — §5.3.2).

Any failing precondition ⇒ REFUSE, print the reason, exit non-zero, write nothing.

### Canonical ceremony message (precondition 1 — nonce binding)

B does NOT sign a bare nonce. B signs exactly:

    cgr-continues.v1|b=<B agent_pk hex>|a_cert=<A cert_hash hex>|nonce=<random>

The script RECONSTRUCTS this string from the passed `--b-key`, `--a-cert-hash` and `--nonce` and
verifies B's signature over it. So the signature proves B consents to continue **this A's cert
specifically**, not merely that B signed some string once.

### Anti-fork is a CHECK, not a CONSTRAINT

Precondition 3 reads the store, finds no edge, then the ceremony writes — a read-then-write with NO
concurrency guard. Two ceremonies run concurrently for the same A could both pass the check and both
write. This is safe today because there is a single operator running this out-of-band, one A at a
time. It is NOT database-enforced uniqueness; a true constraint would be a unique index on the edge
target (or a serialized transaction). Do not mistake this check for enforcement.

Usage (the operator runs this; the repo does not):

    GRAFOMEM_DB_URL=...                  # grafomem store (the edge is written here)
    SUPABASE_URL=https://kaqwkxfaclyqjlfhxrmt.supabase.co   # GEIANT's project (agent_registry)
    SUPABASE_SERVICE_ROLE_KEY=...        # GEIANT service-role key (registry read)
    python scripts/cgr_continues_ceremony.py \
        --tenant <tenant_id> \
        --b-key <B agent_pk hex> --nonce <random> --b-sig <B's sig over the canonical message, hex> \
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


def ceremony_message(b_pubkey_hex: str, a_cert_hash: str, nonce: str) -> str:
    """The canonical message B must sign (precondition 1). BINDS the consent to THIS continuation —
    B's own key and A's cert_hash — so a bare or cross-purpose signature cannot pass."""
    return f"cgr-continues.v1|b={b_pubkey_hex}|a_cert={a_cert_hash}|nonce={nonce}"


# ── preconditions (each returns (ok: bool, reason: str)) ─────────────────────────────────

def check_control_of_b(b_pubkey_hex: str, nonce: str, b_sig_hex: str, a_cert_hash: str) -> tuple[bool, str]:
    """(1) Control of B, BOUND to this ceremony: verify B's signature over the reconstructed canonical
    message (B's key + A's cert_hash + nonce). B is not compromised, so a signature from B is
    meaningful (unlike A — §5.2); binding the message stops a stray B-signature being replayed here."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    msg = ceremony_message(b_pubkey_hex, a_cert_hash, nonce).encode("utf-8")
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(b_pubkey_hex))
        pk.verify(bytes.fromhex(b_sig_hex), msg)
        return True, f"control-of-B OK: B signed the bound message (a_cert={a_cert_hash[:12]}…)"
    except Exception as e:  # noqa: BLE001 — any failure is a refusal
        return False, ("control-of-B FAILED: B's signature over the canonical ceremony message did "
                       f"not verify — signature must be over exactly ceremony_message() ({e})")


def check_a_revoked(supabase_url: str, service_role_key: str, a_agent_pk: str) -> tuple[bool, str]:
    """(2) A genuinely retired: A is revoked in geiant's enforcement index (agent_registry.revoked_at),
    read via the Supabase REST API (PostgREST) with the service-role key — the same access path every
    geiant service uses (SupabaseRegistry / mcp-audit middleware; geiant keeps no raw-Postgres string
    for this project). A `continues` into a LIVE A would be a fork, not a rotation (§5.3.3)."""
    import httpx
    url = supabase_url.rstrip("/") + "/rest/v1/agent_registry"
    headers = {"apikey": service_role_key, "Authorization": f"Bearer {service_role_key}"}
    params = {"agent_pk": f"eq.{a_agent_pk}", "select": "agent_pk,revoked_at", "limit": "1"}
    try:
        resp = httpx.get(url, headers=headers, params=params, timeout=15.0)
        resp.raise_for_status()
        rows = resp.json()
    except Exception as e:  # noqa: BLE001 — a read failure is a refusal (never fail-open)
        return False, f"A-revoked FAILED: geiant agent_registry read error ({e})"
    if not rows:
        return False, f"A-revoked FAILED: {a_agent_pk[:12]}… not found in geiant agent_registry"
    if rows[0].get("revoked_at") is None:
        return False, ("A-revoked FAILED: A.revoked_at IS NULL — A is still live; a continues into a "
                       "live A is a fork, not a rotation")
    return True, f"A is revoked (revoked_at = {rows[0]['revoked_at']})"


def check_anti_fork(store_manager, tenant_id: str, a_cert_hash: str) -> tuple[bool, str]:
    """(3) Anti-fork uniqueness — a CHECK, not a database constraint (see module docstring: TOCTOU,
    single-operator-safe). No existing continues edge already targets A's cert_hash; without this two
    parties could each claim to continue A and split its history (§5.3.4). Foundation-side ledger read
    at issuance — the schema can't see other edges (§3 asymmetry)."""
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

def run_ceremony(*, store_manager, geiant_supabase_url: str, geiant_service_role_key: str,
                 tenant_id: str, b_key: str, nonce: str, b_sig: str, a_agent_pk: str,
                 a_cert_hash: str, decision_date: str,
                 evidence_tier: str = "operator_verification", dry_run: bool = False) -> int:
    """Run all four §5.3 preconditions; write ONE continues-edge record iff all pass. Returns a
    process exit code (0 = written / dry-run-clean; non-zero = refused)."""
    checks = [
        check_control_of_b(b_key, nonce, b_sig, a_cert_hash),
        check_a_revoked(geiant_supabase_url, geiant_service_role_key, a_agent_pk),
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
    ap.add_argument("--nonce", required=True, help="random nonce; B signs ceremony_message() over it")
    ap.add_argument("--b-sig", required=True, help="B's Ed25519 sig over the canonical message (hex)")
    ap.add_argument("--a-agent-pk", required=True, help="predecessor A agent_pk (hex)")
    ap.add_argument("--a-cert-hash", required=True, help="A delegation cert_hash (sha-256 hex)")
    ap.add_argument("--decision-date", required=True, help="date the continuity was determined")
    ap.add_argument("--evidence-tier", default="operator_verification", choices=_EVIDENCE_TIERS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    grafomem_db = os.environ.get("GRAFOMEM_DB_URL")
    supabase_url = os.environ.get("SUPABASE_URL")               # GEIANT's project (agent_registry)
    service_role = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")  # GEIANT service-role key
    if not grafomem_db:
        print("set GRAFOMEM_DB_URL (the store the edge is written to)", file=sys.stderr)
        return 3
    if not (supabase_url and service_role):
        print("set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY for GEIANT's project "
              "(agent_registry read) — geiant's registry is Supabase, not raw Postgres", file=sys.stderr)
        return 3

    from aml.backends.postgres_gmp import PostgresGMPBackend
    from aml.server.stores import StoreManager

    sm = StoreManager(lambda: PostgresGMPBackend(grafomem_db))
    return run_ceremony(
        store_manager=sm, geiant_supabase_url=supabase_url, geiant_service_role_key=service_role,
        tenant_id=args.tenant, b_key=args.b_key, nonce=args.nonce, b_sig=args.b_sig,
        a_agent_pk=args.a_agent_pk, a_cert_hash=args.a_cert_hash,
        decision_date=args.decision_date, evidence_tier=args.evidence_tier, dry_run=args.dry_run,
    )


if __name__ == "__main__":  # pragma: no cover — operator entry point; never imported by the server
    sys.exit(main())
