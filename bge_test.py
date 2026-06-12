from aml.backends.vector_only import _default_embedder
from scripts.run_w8 import recall_by_distance, BINS, _bin_label

print("\n=== W8 EVALUATION: REAL BGE EMBEDDER (Dilution, 1 seed) ===")
embed_fn = _default_embedder()
res = recall_by_distance(embed_fn=embed_fn, seeds=[0])
print("\nW8 recall by distance d (hard, 1 seed) [bge]:\n")
print(f"  {'distance':<16} {'unbounded':>15} {'fifo(K=64)':>15} {'importance(K=64)':>16}  {'summarise(K=64)':>15}")
print("  " + "-" * 78)
for lo, hi in BINS:
    lbl = _bin_label(lo, hi)
    row = [lbl]
    for b in ["unbounded", "fifo(K=64)", "importance(K=64)", "summarise(K=64)"]:
        v = sum(res[(lo, hi)][b]) / len(res[(lo, hi)][b]) if res[(lo, hi)][b] else 0.0
        row.append(f"{v:.3f}")
    print(f"  {row[0]:<16} {row[1]:>15} {row[2]:>15} {row[3]:>16}  {row[4]:>15}")
print("  " + "-" * 78)
