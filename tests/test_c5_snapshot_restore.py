#!/usr/bin/env python3
"""
GRAFOMEM — C5 Restore-then-W6 Probe

Restore-then-W6 probe added to backup runbook to verify snapshots
don't revive committed deletes.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from aml.backends.qdrant_gmp import QdrantGMPBackend
from aml.backends.vector_only import _stub_embedder
from aml.eval.conformance import generate_w6, run_trace, DEFAULT_DIFF, _targets_by_turn, _ts_by_turn, EPS
from aml.eval.metrics import bootstrap_paired_ci
from aml.generator.trace import TurnRole
from statistics import mean

def run_c5_test():
    print("==========================================================")
    print(" GRAFOMEM CLOUD : C5 RESTORE-THEN-W6 PROBE")
    print("==========================================================")
    
    _batch_fn = _stub_embedder(dim=256)
    def single_or_batch_embed(text_or_texts):
        if isinstance(text_or_texts, str):
            return _batch_fn([text_or_texts])[0]
        return _batch_fn(text_or_texts)

    from qdrant_client import QdrantClient, models
    client = QdrantClient("http://127.0.0.1:6333", timeout=60.0)
    collection_name = f"gmp_c5_{time.time_ns()}"
    
    class SingleTenantQdrantBackend(QdrantGMPBackend):
        """W6 is a single-tenant workload."""
        def write(self, content, options):
            options.tenant_id = "default_tenant"
            return super().write(content, options)

        def write_many(self, items):
            for _, opt in items:
                opt.tenant_id = "default_tenant"
            return super().write_many(items)

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

    backend = SingleTenantQdrantBackend(url="http://localhost:6333", collection_name=collection_name, embed_fn=single_or_batch_embed)

    # 1. Run W6 trace to populate and delete records
    print("\n[1/4] Running W6 trace (populating and deleting)...")
    seeds = range(5)
    budget = 512
    
    leak_ps = []
    surv_ps = []
    traces = []
    runs_before = []

    for s in seeds:
        print(f"      - Seed {s}")
        tr = generate_w6(seed=s, difficulty=DEFAULT_DIFF)
        traces.append(tr)
        tgt = _targets_by_turn(tr)
        tsb = _ts_by_turn(tr)
        deleted = tr.ground_truth.deleted_facts
        
        # Run first pass
        run = run_trace(backend, tr, budget_tokens=budget)
        runs_before.append(run)
        
        # 2. Take Snapshot
        print(f"      - Creating snapshot for seed {s}...")
        snapshot_info = client.create_snapshot(collection_name=collection_name)
        snapshot_name = snapshot_info.name
        
        # 3. Drop collection and recover from snapshot
        print(f"      - Dropping collection and recovering from snapshot...")
        c_before = client.count(collection_name).count
        print(f"      - Count before snapshot: {c_before}")
        # Drop the collection
        client.delete_collection(collection_name)
        
        # Recover from snapshot
        client.recover_snapshot(
            collection_name=collection_name,
            location=f"file:///qdrant/snapshots/{collection_name}/{snapshot_name}"
        )
        
        # Verify collection exists again and is ready
        while True:
            if client.collection_exists(collection_name):
                info = client.get_collection(collection_name)
                if info.status == models.CollectionStatus.GREEN:
                    break
            time.sleep(0.1)
            
        c_after = client.count(collection_name).count
        print(f"      - Count after snapshot: {c_after}")
            
        # We need to recreate the backend because the collection might be different/reset
        backend = SingleTenantQdrantBackend(url="http://localhost:6333", collection_name=collection_name, embed_fn=single_or_batch_embed)
        
        # 4. Re-run queries to verify deleted records are still gone
        print(f"      - Re-evaluating queries to verify leakage...")
        leak = []
        surv = []
        
        from aml.backends.interface import RetrieveOptions
        # Find all queries in the trace and re-execute them
        query_turns = [t for s in tr.sessions for t in s.turns if t.role == TurnRole.AGENT_QUERY]
        for qr, turn in zip(run.per_query, query_turns):
            opts = RetrieveOptions(
                budget_tokens=budget,
                as_of=turn.as_of,
                tenant_id="default_tenant",
            )
            # Retrieve from the recovered backend
            mems = backend.retrieve(turn.content, opts)
            retrieved = {m.content for m in mems}
            
            ts = tsb[str(turn.turn_id)]
            deleted_before = {fid for fid, dt in deleted.items() if dt <= ts}
            leak.append(1.0 if (retrieved & deleted_before) else 0.0)
            
            T = tgt.get(str(turn.turn_id), set())
            fid_to_content = {}
            for s in tr.sessions:
                for t in s.turns:
                    for fid in t.introduces:
                        fid_to_content[fid] = t.content
            T_contents = {fid_to_content[fid] for fid in T}
                
            if T_contents and len(retrieved) > 0:
                print(f"      Q: {turn.content}")
                print(f"      Retrieved: {retrieved}")
                print(f"      Target: {T_contents}")
            
            if T:
                surv.append(len(retrieved & T_contents) / len(T_contents))
                
        leak_ps.append(mean(leak))
        surv_ps.append(mean(surv) if surv else float("nan"))
        
        # Cleanup for next seed
        client.delete_collection(collection_name)
        while client.collection_exists(collection_name):
            time.sleep(0.1)
        collection_name = f"gmp_c5_{time.time_ns()}"
        backend = SingleTenantQdrantBackend(url="http://127.0.0.1:6333", collection_name=collection_name, embed_fn=single_or_batch_embed)

    # rec_ps is already computed in the loop: surv_ps.append(sum(rec) / len(rec))
    # so we just use surv_ps directly!
    
    # Calculate BEFORE snapshot recall
    before_rec_per_seed = []
    for r_res, tr in zip(runs_before, traces):
        tgt = _targets_by_turn(tr)
        rec = []
        for qr in r_res.per_query:
            T = tgt.get(qr.turn_id, set())
            if T:
                rec.append(len(qr.retrieved & T) / len(T))
        before_rec_per_seed.append(sum(rec)/len(rec) if rec else float("nan"))
        
    before_m = sum(before_rec_per_seed) / len(before_rec_per_seed)
    
    leak_m = sum(leak_ps) / len(leak_ps)
    surv_m = sum(surv_ps) / len(surv_ps)
    
    print("\n--- RESULTS ---")
    print(f"  Before Snapshot Recall: {before_m:.3f}")
    print(f"  [{'✓' if leak_m <= 0.005 else '✗'}] C5 Restored Leakage: {leak_m:.3f} {'<=' if leak_m <= 0.005 else '>'} 0.005")
    print(f"  [{'✓' if surv_m >= 0.5 else '✗'}] C5 Restored Recall: {surv_m:.3f} {'>=' if surv_m >= 0.5 else '<'} 0.5")


if __name__ == "__main__":
    run_c5_test()
