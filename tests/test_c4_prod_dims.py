#!/usr/bin/env python3
"""
GRAFOMEM — C4 Prod Dims Conformance

Scale/dim re-verification run on production corpus shape (dim=768 and dim=3072).
Tests W2 (Temporal Exactness) and W6 (Deletion Exactness) for Qdrant.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aml.backends.qdrant_gmp import QdrantGMPBackend
from aml.backends.vector_only import _stub_embedder
from aml.eval.conformance import _measure_w2_current, _measure_w2_historical, _measure_w6, EPS
from aml.eval.metrics import bootstrap_paired_ci

def run_tests_for_dim(dim: int):
    print(f"\n==========================================================")
    print(f" TESTING PROD DIMENSION: {dim}")
    print(f"==========================================================")
    
    _batch_fn = _stub_embedder(dim=dim)
    def single_or_batch_embed(text_or_texts):
        if isinstance(text_or_texts, str):
            return _batch_fn([text_or_texts])[0]
        return _batch_fn(text_or_texts)

    class SingleTenantQdrantBackend(QdrantGMPBackend):
        """W2/W6 are single-tenant workloads, so we inject a default tenant to satisfy the mandatory scope."""
        def write(self, content, options):
            if options.tenant_id is None:
                options.tenant_id = "default_tenant"
            return super().write(content, options)

        def retrieve(self, query, options):
            if options.tenant_id is None:
                options.tenant_id = "default_tenant"
            return super().retrieve(query, options)

        def supersede(self, old_ref, content, options):
            if options.tenant_id is None:
                options.tenant_id = "default_tenant"
            return super().supersede(old_ref, content, options)

        def delete(self, ref):
            return super().delete(ref)

    def factory():
        from qdrant_client import QdrantClient
        client = QdrantClient("http://127.0.0.1:6333")
        collection_name = f"gmp_c4_{dim}_{time.time_ns()}"
        return SingleTenantQdrantBackend(url="http://127.0.0.1:6333", collection_name=collection_name, embed_fn=single_or_batch_embed)

    seeds = range(5)
    budget = 512

    # W2 SUPERSESSION_CHAIN (Current)
    print(f"\n--- W2 SUPERSESSION_CHAIN (Current) ---")
    stale_ps, rec_ps = _measure_w2_current(factory, seeds, budget)
    point_s, lo_s, hi_s = bootstrap_paired_ci([0.0]*len(stale_ps), stale_ps)
    point_r, lo_r, hi_r = bootstrap_paired_ci([0.0]*len(rec_ps), rec_ps)
    
    if hi_s <= EPS:
        print(f"  [✓] Stale leakage: {hi_s:.3f} <= {EPS}")
    else:
        print(f"  [✗] Stale leakage: {hi_s:.3f} > {EPS}")
        
    if lo_r >= 0.5:
        print(f"  [✓] Current recall: {lo_r:.3f} >= 0.5")
    else:
        print(f"  [✗] Current recall: {lo_r:.3f} < 0.5")

    # W2 BI_TEMPORAL (Historical)
    print(f"\n--- W2 BI_TEMPORAL (Historical) ---")
    rec_ps_hist = _measure_w2_historical(factory, seeds, budget)
    point_h, lo_h, hi_h = bootstrap_paired_ci([0.0]*len(rec_ps_hist), rec_ps_hist)
    
    if lo_h >= 0.5:
        print(f"  [✓] Historical recall: {lo_h:.3f} >= 0.5")
    else:
        print(f"  [✗] Historical recall: {lo_h:.3f} < 0.5")

    # W6 HARD_DELETE
    print(f"\n--- W6 HARD_DELETE ---")
    leak_ps_w6, surv_ps_w6 = _measure_w6(factory, seeds, budget)
    point_l6, lo_l6, hi_l6 = bootstrap_paired_ci([0.0]*len(leak_ps_w6), leak_ps_w6)
    point_r6, lo_r6, hi_r6 = bootstrap_paired_ci([0.0]*len(surv_ps_w6), surv_ps_w6)

    if hi_l6 <= EPS:
        print(f"  [✓] Deleted leakage: {hi_l6:.3f} <= {EPS}")
    else:
        print(f"  [✗] Deleted leakage: {hi_l6:.3f} > {EPS}")
        
    if lo_r6 >= 0.5:
        print(f"  [✓] Survivor recall: {lo_r6:.3f} >= 0.5")
    else:
        print(f"  [✗] Survivor recall: {lo_r6:.3f} < 0.5")

if __name__ == "__main__":
    run_tests_for_dim(768)
    run_tests_for_dim(3072)
