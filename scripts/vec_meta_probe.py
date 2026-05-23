#!/usr/bin/env python3
"""
v0.2 mechanics probe — does sqlite-vec support the metadata-column pre-filter design?

Verifies, in this exact environment, the four things the backend rewrite depends on:
  1. a vec0 table with metadata columns (tenant_id text, valid_from/valid_until int)
  2. KNN-native filtering: MATCH + k + metadata WHERE conditions, in one query
  3. the sentinel scheme: "" for null tenant, a big int for an open (current) interval,
     filtered with =, <=, > (since IS NULL is unsupported)
  4. UPDATE of a metadata column in place — the supersede operation closes an interval,
     so this MUST work or supersede needs a delete+reinsert fallback.

Standalone: needs `apsw` + `sqlite-vec` only. Run:  python scripts/vec_meta_probe.py
"""

import apsw
import sqlite_vec
from sqlite_vec import serialize_float32

OPEN = 2 ** 62      # open valid_until  (current / not yet superseded)
FROM0 = 0           # null valid_from   (valid from the beginning)
NT = ""             # null tenant       (single-tenant workloads)

conn = apsw.Connection(":memory:")
conn.enableloadextension(True)
conn.loadextension(sqlite_vec.loadable_path())
conn.enableloadextension(False)

# 1. metadata columns on vec0 (text + integer)
try:
    conn.execute(
        "CREATE VIRTUAL TABLE v USING vec0("
        "embedding float[4], tenant_id text, valid_from integer, valid_until integer)"
    )
    print("1. PASS  vec0 with text + integer metadata columns created")
except Exception as e:
    print(f"1. FAIL  cannot create vec0 with metadata columns: {e}")
    raise SystemExit(1)


def ins(rid, emb, tenant, vf, vu):
    conn.execute(
        "INSERT INTO v(rowid, embedding, tenant_id, valid_from, valid_until) "
        "VALUES (?,?,?,?,?)", (rid, serialize_float32(emb), tenant, vf, vu))


# Rome[t0,t1) superseded; Milan[t1,OPEN) current; Naples tenant B; r4 null-tenant
ins(1, [1, 0, 0, 0], "A", 1000, 2000)
ins(2, [1, 0, 0, 0], "A", 2000, OPEN)
ins(3, [0, 1, 0, 0], "B", 1000, OPEN)
ins(4, [0, 0, 1, 0], NT, FROM0, OPEN)

Q = serialize_float32([1, 0, 0, 0])


def knn(extra, params):
    # k as a literal (some sqlite-vec versions reject a bound k); query vec is bound
    sql = ("SELECT rowid FROM v WHERE embedding MATCH ? AND k = 10 "
           + extra + " ORDER BY distance")
    return sorted(r[0] for r in conn.execute(sql, (Q, *params)))


def check(n, got, exp):
    print(f"{n}  {'PASS' if got == exp else 'FAIL'}  got {got}, expected {exp}")


# 2 + 3. KNN-native metadata filtering with the sentinel scheme
check("2. current, tenant A   (valid_until = OPEN) ->",
      knn("AND tenant_id = ? AND valid_until = ?", ("A", OPEN)), [2])
check("3. as_of=1500, tenant A (vfrom<=t<vuntil)   ->",
      knn("AND tenant_id = ? AND valid_from <= ? AND valid_until > ?", ("A", 1500, 1500)), [1])
check("   current, tenant B                        ->",
      knn("AND tenant_id = ? AND valid_until = ?", ("B", OPEN)), [3])
check("   current, null tenant (tenant_id = '')    ->",
      knn("AND tenant_id = ? AND valid_until = ?", (NT, OPEN)), [4])

# 4. UPDATE a metadata column in place (the supersede mechanic)
ins(5, [1, 0, 0, 0], "A", 2000, OPEN)
check("4a. before UPDATE, tenant A current         ->",
      knn("AND tenant_id = ? AND valid_until = ?", ("A", OPEN)), [2, 5])
try:
    conn.execute("UPDATE v SET valid_until = ? WHERE rowid = ?", (3000, 5))
    check("4b. after UPDATE closes r5, A current      ->",
          knn("AND tenant_id = ? AND valid_until = ?", ("A", OPEN)), [2])
except Exception as e:
    print(f"4b. FAIL  metadata UPDATE not supported: {e}")
    print("    -> supersede would need delete+reinsert (store the embedding blob too)")

print("\nIf all PASS: the metadata-column pre-filter design is sound — proceed to the "
      "backend rewrite.\nIf 4b FAILs: supersede needs a delete+reinsert path; everything "
      "else still holds.")
