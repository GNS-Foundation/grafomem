"""
GRAFOMEM batch-ingest bench — single write() vs write_many() on the SQLite store.

The scale probe showed every write is ~10ms and ~all of it is the BGE forward pass
(delete, the one op that doesn't embed, is 0.03ms). This isolates the batched-embedding
lever: write_many() embeds a whole batch in one forward pass under one transaction.
Reports ingest throughput for each path and asserts they yield the same retrievable
store — the fast-path is an accelerator, not a different backend.

Run:  python -m scripts.batch_ingest_bench [N]   (default N=2000)
"""

import sys
from time import perf_counter

from aml.backends.interface import RetrieveOptions, WriteOptions
from aml.backends.sqlite_gmp import SQLiteGMPBackend
from aml.backends.vector_only import _default_embedder

N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000

# Varied synthetic content — distinct enough that embeddings don't collapse and the
# encoder can't trivially cache. Not a workload trace; throughput is what we're measuring.
SUBJ = ["Aria", "Bruno", "Carla", "Dario", "Elena", "Marco", "Nadia", "Paolo"]
VERB = ["lives in", "works in", "studies in", "was born in", "relocated to"]
PLACE = ["Rome", "Milan", "Naples", "Turin", "Bologna", "Florence", "Genoa", "Bari"]


def synth(i: int) -> str:
    return (f"{SUBJ[i % len(SUBJ)]} {VERB[(i // 8) % len(VERB)]} "
            f"{PLACE[(i // 3) % len(PLACE)]} as of record number {i}")


def main() -> None:
    contents = [synth(i) for i in range(N)]
    opts = WriteOptions()

    print(f"GRAFOMEM batch-ingest bench — N={N}, BGE-small\n")
    print("  loading the embedder once (shared across both stores)...")
    embed = _default_embedder()                 # load BGE once; both stores share it

    # --- single write() per item -------------------------------------------
    b1 = SQLiteGMPBackend(":memory:", embed_fn=embed)
    t = perf_counter()
    for c in contents:
        b1.write(c, opts)
    single = perf_counter() - t

    # --- batched write_many() ----------------------------------------------
    b2 = SQLiteGMPBackend(":memory:", embed_fn=embed)
    t = perf_counter()
    b2.write_many([(c, opts) for c in contents])
    batched = perf_counter() - t

    print(f"\n  single  write()   : {single:6.2f}s   {N / single:8.1f} items/s")
    print(f"  batched write_many: {batched:6.2f}s   {N / batched:8.1f} items/s")
    print(f"  speedup           : {single / batched:.1f}x\n")

    # --- equivalence: same retrievable store --------------------------------
    # Compare retrieved sets (order-independent: batched vs single encode can differ by
    # float noise on near-ties, but the *membership* must match — a real divergence would
    # change which rows exist or embed differently enough to fall out of budget).
    q = "Where does Aria live now?"
    r1 = {m.content for m in b1.retrieve(q, RetrieveOptions(budget_tokens=512))}
    r2 = {m.content for m in b2.retrieve(q, RetrieveOptions(budget_tokens=512))}
    assert r1 == r2, f"write_many diverged from single write(): {r1 ^ r2}"

    a1 = [m.content for m in b1.audit()]
    a2 = [m.content for m in b2.audit()]
    assert a1 == a2, "stores differ in contents / order"

    print(f"✓ identical store: {len(a1)} rows, {len(r1)} retrieved for the probe query")
    print("  write_many is N single write()s with the embedder batched — same rows, "
          "same retrieval.")

    b1.close()
    b2.close()


if __name__ == "__main__":
    main()
