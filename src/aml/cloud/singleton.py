"""Cross-replica singleton guard for periodic cycles (Postgres advisory locks).

The UsageReporter and the free-ceiling refresh loop ride the app lifespan, so they run
once *per app instance*. Under horizontal scaling (2+ replicas) that means N concurrent
reporter loops racing the shared ``usage_report_cursor`` — a double-report / double-charge
risk that the per-delta idempotency only partly covers.

``cycle_singleton`` wraps one cycle in a **session-level** ``pg_try_advisory_lock``: exactly
one instance acquires the lock and runs; the rest see it's held and skip that tick. The lock
lives on a dedicated connection held for the cycle, so it releases on ``pg_advisory_unlock``
at cycle end AND automatically if the process crashes (Postgres frees advisory locks when the
session ends). This makes "only one active cycle" true by construction, independent of the
replica count.

Fail-SAFE direction: any lock error ⇒ treat as NOT acquired ⇒ skip the tick. Skipping a
reporter tick never over-charges (reporting is merely delayed); skipping a refresh tick only
leaves the cache staler, and the ceiling check already fail-opens on stale rows.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

import psycopg

logger = logging.getLogger("grafomem.cloud.singleton")

# Advisory-lock namespace (int4) for all grafomem cycle locks — ASCII "gf" (0x6766) — so
# these keys never collide with advisory locks taken elsewhere. Second int4 is the per-cycle id.
_LOCK_NS = 0x6766  # 26470

LOCK_ID_USAGE_REPORTER = 1
LOCK_ID_FREE_CEILING_REFRESH = 2


@contextmanager
def cycle_singleton(db_url: str, lock_id: int, label: str):
    """Yield ``True`` iff this instance won the per-cycle advisory lock.

    Uses a dedicated autocommit connection so lock acquire + release happen on the same
    session and the lock auto-frees on crash. Never raises — on any error it yields
    ``False`` (skip the tick).
    """
    conn = None
    got = False
    try:
        # DEDICATED connection — deliberately NOT from the pool. A session-level advisory
        # lock is bound to its connection; if we used a pooled conn that got returned to the
        # pool mid-cycle, the hold window would break. This conn is held for the whole `with`
        # block (the cycle body runs inside it) and closed in the finally below.
        conn = psycopg.connect(db_url, autocommit=True)
        got = bool(
            conn.execute("SELECT pg_try_advisory_lock(%s, %s)", (_LOCK_NS, lock_id)).fetchone()[0]
        )
        if not got:
            logger.info("%s: another instance holds the singleton lock — skipping tick", label)
    except Exception as e:  # noqa: BLE001 — lock must never break the loop; fail-safe = skip
        logger.warning("%s: advisory-lock acquire failed (skipping tick, fail-safe): %s", label, e)
        got = False
    try:
        yield got
    finally:
        if conn is not None:
            try:
                if got:
                    conn.execute("SELECT pg_advisory_unlock(%s, %s)", (_LOCK_NS, lock_id))
            except Exception:  # noqa: BLE001
                pass  # crash/close would free it anyway
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
