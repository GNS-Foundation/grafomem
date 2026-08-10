"""
Manifold Service for GRAFOMEM Cloud.
Provides the Self-Organizing Map (SOM) training and vectorization pipeline
for rendering the Semantic Manifold in the UI.
"""
from __future__ import annotations
import datetime as dt
import logging
from typing import Any

import numpy as np
import pandas as pd

from aml.cloud.db_pool import RoutingPool
from aml.server.tenant_context import apply_tenant_context

logger = logging.getLogger("grafomem.cloud.manifold")

AGENT_ROLES = ["planner", "retriever", "critic", "executor", "agent"]
WORKFLOWS = ["sprint_planning", "code_review", "deployment_check", "default"]
MODELS = ["mock-model", "opus-4", "sonnet-4", "haiku-4", "gpt-4o", "claude-3-5-sonnet"]
TOOLS = ["search", "read_file", "write_file", "exec", "http", "vector_lookup"]
POLICIES = ["pii_guard", "budget_cap", "tool_allowlist", "rate_limit", "egress_block", "memory_scope", "escalation"]
EMB_DIM = 384
BGE_MODEL = "BAAI/bge-small-en-v1.5"

EXTRACTION_SQL = """
select s.step_id, a.role agent_role, s.workflow_id, s.model_id, s.governance_allowed,
       s.tool_calls, s.governance_logs, s.retrieved_facts,
       s.tokens_used, s.latency_ms, s.step_number, s.created_at,
       s.input_text, s.raw_output, s.parent_decision_id, s.is_synthetic, s.status
from orchestrator_steps s
left join orchestrator_agents a on a.agent_id = s.agent_id
where s.tenant_id = %s
order by s.created_at;
"""

class BgeEmbedder:
    def __init__(self):
        try:
            from fastembed import TextEmbedding
            self._fe = TextEmbedding(model_name=BGE_MODEL)
            self.backend = "fastembed"
        except Exception:
            from sentence_transformers import SentenceTransformer
            self._st = SentenceTransformer(BGE_MODEL)
            self.backend = "sentence-transformers"

    def encode(self, texts, normalize_embeddings=True):
        texts = list(texts)
        if self.backend == "fastembed":
            V = np.asarray(list(self._fe.embed(texts)), float)
        else:
            V = np.asarray(self._st.encode(texts, normalize_embeddings=False), float)
        if normalize_embeddings:
            nrm = np.linalg.norm(V, axis=1, keepdims=True)
            nrm[nrm == 0] = 1.0
            V = V / nrm
        return V

def _onehot(values, vocab):
    idx = {v: i for i, v in enumerate(vocab)}
    M = np.zeros((len(values), len(vocab)))
    for r, v in enumerate(values):
        if v in idx:
            M[r, idx[v]] = 1.0
    return M

def _multihot(lists, vocab):
    idx = {v: i for i, v in enumerate(vocab)}
    M = np.zeros((len(lists), len(vocab)))
    for r, items in enumerate(lists):
        if items is None:
            continue
        for v in items:
            if v in idx:
                M[r, idx[v]] = 1.0
    return M

def _l2(M):
    n = np.linalg.norm(M, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return M / n

def make_about_vectors(df: pd.DataFrame, fact_vec_lookup: dict, model: BgeEmbedder, text_weight: float = 0.3):
    texts = (df.input_text.fillna("") + " " + df.raw_output.fillna("")).tolist()
    text_emb = model.encode(texts, normalize_embeddings=True)
    scores_col = df["retrieval_scores"] if "retrieval_scores" in df else [None] * len(df)
    out = np.zeros((len(df), EMB_DIM))
    for i, (facts, scores) in enumerate(zip(df.retrieved_facts, scores_col)):
        # retrieved_facts elements are dicts {ref:int, content, ...}; the lookup is
        # keyed by the int `ref` (mirrors _compute_manifold_sync). Extract f["ref"].
        vecs = [fact_vec_lookup[f["ref"]] for f in (facts or [])
                if isinstance(f, dict) and f.get("ref") in fact_vec_lookup]
        if vecs:
            V = np.vstack(vecs)
            wts = (np.asarray(scores[:len(vecs)], float) if scores else np.ones(len(vecs)))
            pooled = (V * wts[:, None]).sum(0) / max(wts.sum(), 1e-9)
            pooled /= (np.linalg.norm(pooled) + 1e-9)
            v = pooled + text_weight * text_emb[i]
        else:
            v = text_emb[i]
        out[i] = v / (np.linalg.norm(v) + 1e-9)
    return out

def build_features(df: pd.DataFrame, about: np.ndarray):
    from sklearn.preprocessing import StandardScaler
    cat = np.hstack([
        _onehot(df.model_id, MODELS),
        _onehot(df.agent_role, AGENT_ROLES),
        _onehot(df.workflow_id, WORKFLOWS)
    ])
    policy_lists = []
    for gl in df.governance_logs:
        if gl and isinstance(gl, list):
            policy_lists.append([g.get("policy_name") for g in gl if isinstance(g, dict)])
        else:
            policy_lists.append([])
            
    tool_lists = []
    for tc in df.tool_calls:
        if tc and isinstance(tc, list):
            tool_lists.append([t.get("name") if isinstance(t, dict) else t for t in tc])
        else:
            tool_lists.append([])
    
    multi = np.hstack([
        _multihot(tool_lists, TOOLS),
        _multihot(policy_lists, POLICIES)
    ])
    
    num_df = df[["tokens_used", "latency_ms", "step_number"]].copy()
    num_df.fillna(0, inplace=True)
    num = StandardScaler().fit_transform(num_df.to_numpy(float))
    
    blocks = [(_l2(about), 3.0), (_l2(multi), 1.0), (_l2(cat), 0.7), (_l2(num), 0.5)]
    X = np.hstack([B * np.sqrt(wt) for B, wt in blocks])
    return X.astype(float)

def train_som(X: np.ndarray, seed: int = 42):
    from minisom import MiniSom
    n = X.shape[0]
    side = max(6, int(round(np.sqrt(5 * np.sqrt(n)))))
    som = MiniSom(side, side, X.shape[1], sigma=1.0, learning_rate=0.5, random_seed=seed)
    som.random_weights_init(X)
    som.train_random(X, 500)
    bmu = np.array([som.winner(x) for x in X])
    return som, side, bmu, som.get_weights()

def serialize_manifold(df: pd.DataFrame, bmu: np.ndarray, side: int, source: str = "synthetic",
                       som_version: str = "unknown", vectors_matched: int = 0,
                       vectors_requested: int = 0) -> dict[str, Any]:
    hex_px = 60
    LENSES = ["compliance", "latency", "failover", "loop", "timeout"]
    d = df.reset_index(drop=True).copy()
    d["_q"] = bmu[:, 0]
    d["_r"] = bmu[:, 1]
    d["_cell"] = [f"c_{int(a):02d}_{int(b):02d}" for a, b in zip(d["_q"], d["_r"])]

    cells = []
    for (cq, cr), g in d.groupby(["_q", "_r"]):
        pols = [p.get("policy_name") for gl in g.governance_logs if gl for p in gl if isinstance(p, dict)]
        top_pol = pd.Series(pols).value_counts().index[0] if pols else "—"
        aroles = g.agent_role.value_counts()
        top_agent = aroles.index[0] if len(aroles) > 0 else "unknown"
        
        x = (cq + 0.5 * (cr % 2)) * hex_px
        y = cr * (np.sqrt(3) / 2) * hex_px
        
        cells.append(dict(
            id=f"c_{int(cq):02d}_{int(cr):02d}", q=int(cq), r=int(cr),
            x=round(float(x), 1), y=round(float(y), 1), count=int(len(g)),
            label=f"agent:{top_agent} · policy:{top_pol}",
            exemplars=g.step_id.head(8).tolist(),
            lenses=dict(
                compliance=round(float(g.governance_allowed.mean()), 3) if not g.governance_allowed.isna().all() else 1.0,
                latency=round(float(g.latency_ms.mean()), 1) if not g.latency_ms.isna().all() else 0.0,
                failover=int((g.status == "failed_failover").sum()) if "status" in g else 0,
                loop=int((g.status == "halted_loop").sum()) if "status" in g else 0,
                timeout=int((g.status == "failed_timeout").sum()) if "status" in g else 0,
            ),
        ))

    steps = []
    for _, row in d.iterrows():
        steps.append(dict(
            stepId=row["step_id"],
            cellId=row["_cell"],
            governanceAllowed=bool(row["governance_allowed"]),
            agentRole=row["agent_role"] if pd.notna(row["agent_role"]) else "unknown",
            workflowId=row["workflow_id"] if pd.notna(row["workflow_id"]) else "unknown",
            modelId=row["model_id"] if pd.notna(row["model_id"]) else "unknown",
            createdAt=pd.Timestamp(row["created_at"]).isoformat(),
            status=row.get("status", "completed") if pd.notna(row.get("status")) else "completed",
            inputText=row.get("input_text", "") or "",
            toolCalls=[{"name": t} for t in (row.get("tool_calls", []) or [])],
            governanceLogs=[{"policy_name": g.get("policy_name"), "allowed": (g.get("result") == "allowed" if "result" in g else g.get("allowed", False))} for g in (row.get("governance_logs", []) or [])]
        ))

    edges = []
    if "parent_decision_id" in d:
        for sid, p in zip(d["step_id"], d["parent_decision_id"]):
            if isinstance(p, str) and p:
                edges.append({"from": p, "to": sid, "kind": "parent"})

    synthetic_count = int(d["is_synthetic"].sum()) if "is_synthetic" in d else 0
    real_count = int(len(d) - synthetic_count)
    if synthetic_count == 0:
        steps_source = "real"
        generator = None
    elif real_count == 0:
        steps_source = "synthetic"
        generator = "som_seed@latest"
    else:
        steps_source = "mixed"
        generator = "som_seed@latest"

    provenance = dict(
        # `source` reflects reality: "real-vectors" when fact embeddings resolved,
        # "text-only" on fallback, "none" when there were no steps. `matched`/
        # `requested` are the diagnostic whose absence hid the fact_ref bug.
        # NOTE: only vectors.source (+ meta.source) change here — steps.source below
        # is a separate, FE-consumed field and is left untouched.
        vectors=dict(source=source, model="BAAI/bge-small-en-v1.5", dim=384,
                     matched=int(vectors_matched), requested=int(vectors_requested)),
        steps=dict(
            source=steps_source,
            real_count=real_count,
            synthetic_count=synthetic_count,
            generator=generator,
            schema_mirror="orchestrator_steps"
        )
    )

    manifold = dict(
        meta=dict(
            version="0.1.0",
            somVersion=som_version,
            generatedAt=dt.datetime.utcnow().isoformat() + "Z",
            source=source,
            somGrid=[int(side), int(side)],
            nSteps=int(len(d)),
            lenses=LENSES
        ),
        cells=cells,
        steps=steps,
        edges=edges,
        hex_px=hex_px,
        generated_at=dt.datetime.utcnow().isoformat() + "Z",
        provenance=provenance
    )
    return manifold

class ManifoldService:
    # /field cache — IN-MEMORY per process, short TTL. Deploy-safe by construction: a
    # new deploy is a new process → empty cache → recompute (no persistent DB cache, so
    # NOT the manifold_cache stale-after-deploy class of bug). TTL bounds staleness.
    _FIELD_TTL_S = 60.0
    _FIELD_MAX_POINTS = 20000        # safety cap on the tenant-scoped embedding scan

    def __init__(self, db_url: str, pool: RoutingPool | None = None):
        self.db_url = db_url
        self.pool = pool
        self._embedder = None
        self._worker_thread = None
        self._stop_event = None
        self._field_cache: dict[str, tuple[dict, float]] = {}  # tenant → (payload, expiry_monotonic)

    def ensure_schema(self):
        import psycopg2
        if self.pool:
            conn = self.pool.getconn(); apply_tenant_context(conn)
        else:
            conn = psycopg2.connect(self.db_url); apply_tenant_context(conn)
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS manifold_cache (
                        tenant_id TEXT PRIMARY KEY,
                        payload JSONB NOT NULL,
                        updated_at TIMESTAMP DEFAULT NOW(),
                        som_version TEXT,
                        som_weights BYTEA
                    )
                """)
            conn.commit()
            
            with conn.cursor() as cur:
                # Migrate tables created before these columns existed. ADD COLUMN
                # IF NOT EXISTS is idempotent and works under autocommit — the pooled
                # connection runs with autocommit=True, so SAVEPOINT (which requires an
                # open transaction block) is unavailable here.
                cur.execute("ALTER TABLE manifold_cache ADD COLUMN IF NOT EXISTS som_version TEXT;")
                cur.execute("ALTER TABLE manifold_cache ADD COLUMN IF NOT EXISTS som_weights BYTEA;")
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to setup manifold_cache table: {e}")
            conn.rollback()
        finally:
            if self.pool:
                self.pool.putconn(conn)
            else:
                conn.close()

    def start_background_worker(self, interval_seconds: int = 180):
        import threading
        import time
        import json
        
        if self._worker_thread and self._worker_thread.is_alive():
            return
            
        self._stop_event = threading.Event()
        
        def worker():
            logger.info("Manifold background worker started.")
            # The worker is started during app setup, BEFORE the deferred
            # ensure_schema DB-init step runs. Without ensuring our schema here
            # first, early cache writes hit a not-yet-migrated manifold_cache
            # table and raise "column som_version does not exist" until the
            # deferred step catches up. ensure_schema is idempotent, so call it
            # once up front to close that race (incl. on a first/fresh deploy).
            try:
                self.ensure_schema()
            except Exception as e:
                logger.warning(f"Manifold worker pre-flight ensure_schema failed: {e}")
            while not self._stop_event.is_set():
                try:
                    import psycopg2
                    if self.pool:
                        conn = self.pool.getconn(); apply_tenant_context(conn)
                    else:
                        conn = psycopg2.connect(self.db_url); apply_tenant_context(conn)
                        
                    tenants = ["default"]
                    try:
                        with conn.cursor() as cur:
                            cur.execute("SELECT DISTINCT tenant_id FROM orchestrator_steps")
                            tenants = [row[0] if isinstance(row, tuple) else row["tenant_id"] for row in cur.fetchall()]
                    except Exception as e:
                        logger.error(f"Failed to fetch active tenants: {e}")
                        conn.rollback()

                    for tenant_id in tenants:
                        try:
                            payload, som_version, som_weights = self._compute_manifold_sync(tenant_id)
                            with conn.cursor() as cur:
                                cur.execute("""
                                    INSERT INTO manifold_cache (tenant_id, payload, updated_at, som_version, som_weights)
                                    VALUES (%s, %s, NOW(), %s, %s)
                                    ON CONFLICT (tenant_id) DO UPDATE SET 
                                    payload = EXCLUDED.payload, 
                                    updated_at = NOW(),
                                    som_version = EXCLUDED.som_version,
                                    som_weights = EXCLUDED.som_weights
                                """, (tenant_id, json.dumps(payload), som_version, som_weights if self.pool else psycopg2.Binary(som_weights)))
                            conn.commit()
                            logger.info(f"Manifold background cache updated for {tenant_id}.")
                        except Exception as e:
                            logger.error(f"Error saving manifold cache for {tenant_id}: {e}")
                            conn.rollback()
                    
                    if self.pool:
                        self.pool.putconn(conn)
                    else:
                        conn.close()
                            
                except Exception as e:
                    logger.error(f"Manifold worker iteration failed: {e}")
                
                # Sleep in short bursts to allow clean exit
                for _ in range(interval_seconds):
                    if self._stop_event.is_set():
                        break
                    time.sleep(1)
                    
        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    def stop_background_worker(self):
        if self._stop_event:
            self._stop_event.set()
        if self._worker_thread:
            self._worker_thread.join(timeout=5.0)

    @property
    def embedder(self) -> BgeEmbedder:
        if self._embedder is None:
            self._embedder = BgeEmbedder()
        return self._embedder

    # ──────────────────────────────────────────────────────────────────
    # /v1/manifold/field — redesigned view, re-sourced from decision_embeddings.
    # Read-only, tenant-scoped (RLS). Independent of export/locate/serialize_manifold.
    # ──────────────────────────────────────────────────────────────────

    def field(self, tenant_id: str, decision_trail, store_manager) -> dict[str, Any]:
        """PCA(decision_embeddings) → 2-D coords + a kernel-Beta CGR-outcome field +
        per-agent aggregates. Cached in-memory per tenant with a short TTL (deploy-safe;
        a new process starts empty). `decision_trail`/`store_manager` are the substrate
        loader's deps (passed from the route's app.state)."""
        import time
        now = time.monotonic()
        cached = self._field_cache.get(tenant_id)
        if cached and cached[1] > now:
            return cached[0]
        payload = self._compute_field(tenant_id, decision_trail, store_manager)
        self._field_cache[tenant_id] = (payload, now + self._FIELD_TTL_S)
        return payload

    def _empty_field(self, tenant_id: str, n_points: int, note: str) -> dict[str, Any]:
        return {
            "points": [], "agents": [],
            "field": {"resolution": 0, "x_range": [0, 0], "y_range": [0, 0],
                      "bandwidth": 0.0, "grid": [], "support": []},
            "variance_explained": [0.0, 0.0],
            "meta": {"dimension": "receivables", "n_points": n_points, "n_resolved": 0,
                     "source": "decision_embeddings",
                     "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "note": note},
        }

    def _compute_field(self, tenant_id: str, decision_trail, store_manager) -> dict[str, Any]:
        import psycopg2
        from collections import defaultdict
        from aml.cgr.substrate import load_substrate
        from aml.cgr.engine import compute_scores_from_rows
        from aml.cloud.manifold_field import pca_2d, kernel_beta_field

        # 1) Embeddings — ONE RLS-scoped bulk query (app.current_tenant scopes rows).
        emb: dict[str, list[float]] = {}
        if self.pool:
            conn = self.pool.getconn(); apply_tenant_context(conn)
        else:
            conn = psycopg2.connect(self.db_url); apply_tenant_context(conn)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT decision_id, embedding::text FROM decision_embeddings "
                    "WHERE tenant_id = %s AND valid_until IS NULL LIMIT %s",
                    (tenant_id, self._FIELD_MAX_POINTS),
                )
                for did, vec in cur.fetchall():
                    try:
                        emb[did] = [float(x) for x in str(vec).strip("[]").split(",")]
                    except Exception:
                        pass
        finally:
            if self.pool:
                self.pool.putconn(conn)
            else:
                conn.close()

        if not emb:
            return self._empty_field(tenant_id, 0, "no decision embeddings for tenant")

        # 2) Outcome + agent join — SINGLE bulk pass (load_substrate reads all outcomes
        #    once + all decisions in one query; NOT per-decision N+1). CGR posteriors
        #    reuse those same rows (no extra reads).
        rows = load_substrate(decision_trail, store_manager, tenant_id, limit=self._FIELD_MAX_POINTS)
        cgr_by_handle = {r.agent_handle: r for r in compute_scores_from_rows(rows)}

        # 3) Join decisions↔embeddings by decision_id; drop non-finite vectors.
        pts, vecs = [], []
        for r in rows:
            v = emb.get(r.decision_id)
            if v is None:
                continue
            pts.append(r); vecs.append(v)
        if len(pts) < 3:
            return self._empty_field(tenant_id, len(pts), "fewer than 3 embedded decisions (thin geometry)")
        X = np.asarray(vecs, dtype=float)
        finite = np.isfinite(X).all(axis=1)
        if not finite.all():
            X = X[finite]; pts = [p for p, f in zip(pts, finite) if f]
        if len(pts) < 3:
            return self._empty_field(tenant_id, len(pts), "fewer than 3 finite embeddings")

        # 4) PCA → coords (sign-pinned, deterministic) + variance.
        coords, var = pca_2d(X)
        outcome = [p.outcome for p in pts]
        paid = np.array([1 if o == "paid" else 0 for o in outcome])
        resolved = np.array([o in ("paid", "default") for o in outcome])

        # 5) Kernel-Beta outcome-posterior field over resolved points.
        field = kernel_beta_field(coords, paid, resolved)

        points = [{
            "decision_id": p.decision_id,
            "x": round(float(coords[i, 0]), 4), "y": round(float(coords[i, 1]), 4),
            "outcome": p.outcome, "agent_handle": p.agent_handle, "agent_key": p.agent_key,
            "verifiability_tag": p.verifiability_tag, "decision": p.decision,
        } for i, p in enumerate(pts)]

        # 6) Per-agent aggregate: centroid of the agent's decisions + CGR standing.
        by_agent: dict[str, list[int]] = defaultdict(list)
        for i, p in enumerate(pts):
            if p.agent_handle:
                by_agent[p.agent_handle].append(i)
        agents = []
        for handle, idxs in by_agent.items():
            c = coords[idxs].mean(axis=0)
            res = cgr_by_handle.get(handle)
            agents.append({
                "agent_handle": handle,
                "x": round(float(c[0]), 4), "y": round(float(c[1]), 4),
                "n_decisions": len(idxs),
                "cgr_score": (round(float(res.cgr_score), 4) if res else None),
                "capability_tier": (res.capability_tier if res else None),
                "n_resolved": (res.n_resolved if res else 0),
                "confidence": (round(float(res.confidence), 4) if res else None),
            })
        agents.sort(key=lambda a: (a["cgr_score"] is not None, a["cgr_score"] or 0), reverse=True)

        return {
            "points": points,
            "field": field,
            "agents": agents,
            "variance_explained": [round(float(var[0]), 4), round(float(var[1]), 4)],
            "meta": {
                "dimension": "receivables",
                "n_points": len(points),
                "n_resolved": int(resolved.sum()),
                "source": "decision_embeddings",
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        }

    def generate_manifold(self, tenant_id: str) -> dict[str, Any]:
        """Fetch precomputed manifold from cache, fallback to sync compute if missing."""
        import psycopg2
        import json
        if self.pool:
            conn = self.pool.getconn(); apply_tenant_context(conn)
        else:
            conn = psycopg2.connect(self.db_url); apply_tenant_context(conn)
            
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM manifold_cache WHERE tenant_id = %s", (tenant_id,))
                row = cur.fetchone()
                if row and row[0]:
                    return row[0] if isinstance(row[0], dict) else json.loads(row[0])
        except Exception as e:
            logger.error(f"Failed to read manifold cache: {e}")
            conn.rollback()
        finally:
            if self.pool:
                self.pool.putconn(conn)
            else:
                conn.close()
                
        # Fallback to sync computation if no cache exists yet
        logger.info(f"No manifold cache found for tenant {tenant_id}, computing synchronously...")
        payload, som_version, som_weights = self._compute_manifold_sync(tenant_id)
        
        # Save it to cache for next time
        try:
            if self.pool:
                conn = self.pool.getconn(); apply_tenant_context(conn)
            else:
                conn = psycopg2.connect(self.db_url); apply_tenant_context(conn)
            with conn.cursor() as cur:
                import psycopg2
                cur.execute("""
                    INSERT INTO manifold_cache (tenant_id, payload, updated_at, som_version, som_weights)
                    VALUES (%s, %s, NOW(), %s, %s)
                    ON CONFLICT (tenant_id) DO UPDATE SET 
                    payload = EXCLUDED.payload, 
                    updated_at = NOW(),
                    som_version = EXCLUDED.som_version,
                    som_weights = EXCLUDED.som_weights
                """, (tenant_id, json.dumps(payload), som_version, som_weights if self.pool else psycopg2.Binary(som_weights)))
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to save fallback cache: {e}")
            if 'conn' in locals() and conn: conn.rollback()
        finally:
            if 'conn' in locals() and conn:
                if self.pool:
                    self.pool.putconn(conn)
                else:
                    conn.close()
                    
        return payload

    def _compute_manifold_sync(self, tenant_id: str) -> tuple[dict[str, Any], str, bytes]:
        """Fetch data from PostgreSQL, train SOM, and return (payload, som_version, som_weights)."""
        import psycopg2
        
        df = None
        if self.pool:
            conn = self.pool.getconn(); apply_tenant_context(conn)
            try:
                with conn.cursor() as cur:
                    cur.execute(EXTRACTION_SQL, (tenant_id,))
                    rows = cur.fetchall()
                df = pd.DataFrame(rows)
            except Exception as e:
                logger.error(f"Failed to extract manifold data: {e}")
                raise
            finally:
                self.pool.putconn(conn)
        else:
            conn = psycopg2.connect(self.db_url); apply_tenant_context(conn)
            try:
                df = pd.read_sql(EXTRACTION_SQL, conn, params=(tenant_id,))
            finally:
                conn.close()
                
        if df is None or len(df) == 0:
            som_version = dt.datetime.utcnow().isoformat() + "Z"
            empty_df = pd.DataFrame(columns=["step_id", "agent_role", "workflow_id", "model_id", "governance_allowed", "tool_calls", "governance_logs", "retrieved_facts", "tokens_used", "latency_ms", "step_number", "created_at", "input_text", "raw_output", "parent_decision_id", "status", "is_synthetic"])
            # No steps at all → no vectors of any kind; distinct from the text-only fallback.
            payload = serialize_manifold(empty_df, np.zeros((0, 2)), 6, source="none",
                                         som_version=som_version, vectors_matched=0, vectors_requested=0)
            return payload, som_version, b""
            
        # We need a new connection for embeddings because the first one is already closed/returned
        if self.pool:
            conn = self.pool.getconn(); apply_tenant_context(conn)
        else:
            conn = psycopg2.connect(self.db_url); apply_tenant_context(conn)
            
        try:
            # retrieved_facts is a JSONB array of dicts {ref:int, content, ...}; extract
            # the int refs (memory_embeddings.ref BIGINT PK). The old isinstance(r, str)
            # filter dropped every dict → refs empty → fact-vectors never loaded.
            refs = sorted({r["ref"] for fs in df.retrieved_facts for r in (fs or [])
                           if isinstance(r, dict) and isinstance(r.get("ref"), int)})
            lookup = {}
            if refs:
                cur = conn.cursor()
                try:
                    cur.execute("select ref, embedding::text from memory_embeddings where ref = any(%s)", (refs,))
                    for row in cur.fetchall():
                        # Handle both dict_row and tuple row
                        k = row["ref"] if isinstance(row, dict) else row[0]
                        v = row["embedding"] if isinstance(row, dict) else row[1]
                        vec_list = [float(x) for x in str(v).strip("[]").split(",")]
                        lookup[k] = np.asarray(vec_list, float)
                except Exception as e:
                    logger.warning(f"Failed to load fact embeddings: {e}. Falling back to text-only.")
                    if hasattr(conn, "rollback"): conn.rollback()
        finally:
            if self.pool:
                self.pool.putconn(conn)
            else:
                conn.close()

        about = make_about_vectors(df, lookup, self.embedder)
        X = build_features(df, about)

        som, side, bmu, som_weights = train_som(X)
        som_version = dt.datetime.utcnow().isoformat() + "Z"
        # Honest provenance: real fact-vectors only if any resolved; else text-only.
        matched, requested = len(lookup), len(refs)
        source = "real-vectors" if matched > 0 else "text-only"
        logger.info("manifold vectors: %d/%d fact refs resolved (source=%s)", matched, requested, source)
        payload = serialize_manifold(df, bmu, side, source=source, som_version=som_version,
                                     vectors_matched=matched, vectors_requested=requested)
        return payload, som_version, som_weights.tobytes()

    def locate_step(self, step_id: str, tenant_id: str) -> dict[str, Any]:
        """Dynamically compute the SOM cell placement for a given step using cached weights."""
        import psycopg2
        import json
        if self.pool:
            conn = self.pool.getconn(); apply_tenant_context(conn)
        else:
            conn = psycopg2.connect(self.db_url); apply_tenant_context(conn)
            
        try:
            # 1. Load the step
            query = EXTRACTION_SQL.replace("order by s.created_at;", "and s.step_id = %s")
            df = pd.read_sql(query, conn, params=(tenant_id, step_id))
            if len(df) == 0:
                return {"error": "Step not found"}
                
            # 2. Build features X
            refs = sorted({r["ref"] for fs in df.retrieved_facts for r in (fs or [])
                           if isinstance(r, dict) and isinstance(r.get("ref"), int)})
            lookup = {}
            if refs:
                cur = conn.cursor()
                try:
                    cur.execute("select ref, embedding::text from memory_embeddings where ref = any(%s)", (refs,))
                    for k, v in cur.fetchall():
                        vec_list = [float(x) for x in str(v).strip("[]").split(",")]
                        lookup[k] = np.asarray(vec_list, float)
                except Exception as e:
                    logger.warning(f"Failed to load fact embeddings: {e}. Falling back to text-only.")
                    conn.rollback()
                    
            about = make_about_vectors(df, lookup, self.embedder)
            X = build_features(df, about)
            
            # 3. Load som_weights from manifold_cache
            cur = conn.cursor()
            cur.execute("SELECT payload, som_version, som_weights FROM manifold_cache WHERE tenant_id = %s", (tenant_id,))
            row = cur.fetchone()
            if not row or not row[1] or not row[2]:
                return {"error": "Manifold not trained yet"}
                
            payload = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            som_version = row[1]
            som_weights_bytes = row[2]
            
            side = payload["meta"]["somGrid"][0]
            feature_dim = X.shape[1]
            
            weights = np.frombuffer(som_weights_bytes, dtype=float).reshape(side, side, feature_dim)
            
            # 4. Compute BMU
            from minisom import MiniSom
            som = MiniSom(side, side, feature_dim)
            som._weights = weights
            winner = som.winner(X[0])
            
            # 5. Format return
            cq, cr = winner
            cellId = f"c_{int(cq):02d}_{int(cr):02d}"
            
            return {
                "stepId": step_id,
                "cellId": cellId,
                "somVersion": som_version
            }
            
        except Exception as e:
            logger.error(f"Failed to locate step: {e}")
            return {"error": str(e)}
        finally:
            if self.pool:
                self.pool.putconn(conn)
            else:
                conn.close()
