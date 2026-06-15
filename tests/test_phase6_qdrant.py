#!/usr/bin/env python3
"""
GRAFOMEM — Phase 6 Qdrant Conformance (R2)

Runs W5 (MULTI_TENANT) against Qdrant in two modes:
- 7a (Normal): Adapter correctly drives scope. Leakage must be <= EPS.
- 7b (Dropped Scope): We simulate the engine vulnerability by omitting the tenant.
  The adapter MUST structurally refuse to omit the scope, resulting in leak 0.00.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aml.backends.qdrant_gmp import QdrantGMPBackend
from aml.backends.vector_only import _stub_embedder
from aml.eval.conformance import _measure_w5, EPS

_batch_fn = _stub_embedder(dim=256)
def single_or_batch_embed(text_or_texts):
    if isinstance(text_or_texts, str):
        return _batch_fn([text_or_texts])[0]
    return _batch_fn(text_or_texts)

def factory():
    from qdrant_client import QdrantClient
    client = QdrantClient("http://localhost:6333")
    collection_name = f"gmp_conformance_{time.time_ns()}"
    return QdrantGMPBackend(url="http://localhost:6333", collection_name=collection_name, embed_fn=single_or_batch_embed)

class DroppedScopeQdrantBackend(QdrantGMPBackend):
    """Simulates 7b dropped scope from the harness's perspective."""
    def retrieve(self, query, options):
        # Force drop the scope on retrieval
        options.tenant_id = None
        return super().retrieve(query, options)

def dropped_scope_factory():
    from qdrant_client import QdrantClient
    client = QdrantClient("http://localhost:6333")
    collection_name = f"gmp_conformance_dropped_{time.time_ns()}"
    return DroppedScopeQdrantBackend(url="http://localhost:6333", collection_name=collection_name, embed_fn=single_or_batch_embed)


def run_tests():
    print("==========================================================")
    print(" GRAFOMEM CLOUD : PHASE 6 QDRANT TENANCY (R2 GATE)")
    print("==========================================================")
    seeds = range(5)
    
    # 7a: Normal W5
    print("\n--- TEST 7a: Adapter drives scope CORRECTLY ---")
    leak_ps, rec_ps = _measure_w5(factory, seeds, 512)
    from statistics import mean
    from aml.eval.metrics import bootstrap_paired_ci
    point, lo, hi = bootstrap_paired_ci([0.0]*len(leak_ps), leak_ps)
    if hi <= EPS:
        print(f"  [✓] 7a passed. Leakage: {hi:.3f} <= {EPS}")
    else:
        print(f"  [✗] 7a failed. Leakage: {hi:.3f} > {EPS}")

    # 7b: Dropped scope W5
    print("\n--- TEST 7b: Scope DELIBERATELY dropped ---")
    # In 7b, because the adapter refuses to drop scope, it should return 0 results
    # so leak will be 0.00. 
    try:
        leak_ps_dropped, rec_ps_dropped = _measure_w5(dropped_scope_factory, seeds, 512)
        point_d, lo_d, hi_d = bootstrap_paired_ci([0.0]*len(leak_ps_dropped), leak_ps_dropped)
        
        if hi_d <= EPS:
            print(f"  [✓] 7b passed, 0 skipped. Leakage: {hi_d:.3f} (adapter REFUSES to drop scope)")
        else:
            print(f"  [✗] 7b failed. Leakage: {hi_d:.3f}")
    except Exception as e:
        print(f"  [✗] 7b failed with exception: {e}")

if __name__ == "__main__":
    run_tests()
