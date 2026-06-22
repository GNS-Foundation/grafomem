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
        vecs = [fact_vec_lookup[f] for f in (facts or []) if f in fact_vec_lookup]
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

def serialize_manifold(df: pd.DataFrame, bmu: np.ndarray, side: int, source: str = "synthetic", som_version: str = "unknown") -> dict[str, Any]:
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
        vectors=dict(source=source, model="BAAI/bge-small-en-v1.5", dim=384),
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
    def __init__(self, db_url: str, pool: RoutingPool | None = None):
        self.db_url = db_url
        self.pool = pool
        self._embedder = None
        self._worker_thread = None
        self._stop_event = None

    def ensure_schema(self):
        import psycopg2
        if self.pool:
            conn = self.pool.getconn()
        else:
            conn = psycopg2.connect(self.db_url)
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
                try:
                    cur.execute("SAVEPOINT manifold_update1")
                    cur.execute("ALTER TABLE manifold_cache ADD COLUMN som_version TEXT;")
                    cur.execute("RELEASE SAVEPOINT manifold_update1")
                except Exception as e:
                    logger.warning(f"Could not add som_version: {e}")
                    cur.execute("ROLLBACK TO SAVEPOINT manifold_update1")
                    
                try:
                    cur.execute("SAVEPOINT manifold_update2")
                    cur.execute("ALTER TABLE manifold_cache ADD COLUMN som_weights BYTEA;")
                    cur.execute("RELEASE SAVEPOINT manifold_update2")
                except Exception as e:
                    logger.warning(f"Could not add som_weights: {e}")
                    cur.execute("ROLLBACK TO SAVEPOINT manifold_update2")
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
            while not self._stop_event.is_set():
                try:
                    import psycopg2
                    if self.pool:
                        conn = self.pool.getconn()
                    else:
                        conn = psycopg2.connect(self.db_url)
                        
                    tenants = ["default"]
                    try:
                        with conn.cursor() as cur:
                            cur.execute("SELECT DISTINCT tenant_id FROM orchestrator_steps")
                            tenants = [row[0] for row in cur.fetchall()]
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
                                """, (tenant_id, json.dumps(payload), som_version, psycopg2.Binary(som_weights)))
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

    def generate_manifold(self, tenant_id: str) -> dict[str, Any]:
        """Fetch precomputed manifold from cache, fallback to sync compute if missing."""
        import psycopg2
        import json
        if self.pool:
            conn = self.pool.getconn()
        else:
            conn = psycopg2.connect(self.db_url)
            
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
                conn = self.pool.getconn()
            else:
                conn = psycopg2.connect(self.db_url)
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO manifold_cache (tenant_id, payload, updated_at, som_version, som_weights)
                    VALUES (%s, %s, NOW(), %s, %s)
                    ON CONFLICT (tenant_id) DO UPDATE SET 
                    payload = EXCLUDED.payload, 
                    updated_at = NOW(),
                    som_version = EXCLUDED.som_version,
                    som_weights = EXCLUDED.som_weights
                """, (tenant_id, json.dumps(payload), som_version, psycopg2.Binary(som_weights)))
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
        # Using a direct psycopg2 connection for pandas.read_sql
        import psycopg2
        if self.pool:
            conn = self.pool.getconn()
        else:
            conn = psycopg2.connect(self.db_url)
            
        try:
            df = pd.read_sql(EXTRACTION_SQL, conn, params=(tenant_id,))
            
            if len(df) == 0:
                # Return empty manifold if no data
                som_version = dt.datetime.utcnow().isoformat() + "Z"
                empty_df = pd.DataFrame(columns=["step_id", "agent_role", "workflow_id", "model_id", "governance_allowed", "tool_calls", "governance_logs", "retrieved_facts", "tokens_used", "latency_ms", "step_number", "created_at", "input_text", "raw_output", "parent_decision_id", "status", "is_synthetic"])
                payload = serialize_manifold(empty_df, np.zeros((0, 2)), 6, source="live", som_version=som_version)
                return payload, som_version, b""
            
            # Fetch embeddings for facts
            refs = sorted({r for fs in df.retrieved_facts for r in (fs or []) if isinstance(r, str)})
            lookup = {}
            if refs:
                cur = conn.cursor()
                # Use correct table - assume memory_embeddings exists based on probe
                try:
                    cur.execute("select fact_ref, embedding::text from memory_embeddings where fact_ref = any(%s)", (refs,))
                    for k, v in cur.fetchall():
                        vec_list = [float(x) for x in str(v).strip("[]").split(",")]
                        lookup[k] = np.asarray(vec_list, float)
                except Exception as e:
                    logger.warning(f"Failed to load fact embeddings: {e}. Falling back to text-only.")
                    conn.rollback() # Rollback the failed transaction block
            
            about = make_about_vectors(df, lookup, self.embedder)
            X = build_features(df, about)
            
            som, side, bmu, som_weights = train_som(X)
            som_version = dt.datetime.utcnow().isoformat() + "Z"
            payload = serialize_manifold(df, bmu, side, source="live", som_version=som_version)
            return payload, som_version, som_weights.tobytes()
            
        finally:
            if self.pool:
                self.pool.putconn(conn)
            else:
                conn.close()

    def locate_step(self, step_id: str, tenant_id: str) -> dict[str, Any]:
        """Dynamically compute the SOM cell placement for a given step using cached weights."""
        import psycopg2
        import json
        if self.pool:
            conn = self.pool.getconn()
        else:
            conn = psycopg2.connect(self.db_url)
            
        try:
            # 1. Load the step
            query = EXTRACTION_SQL.replace("order by s.created_at;", "and s.step_id = %s")
            df = pd.read_sql(query, conn, params=(tenant_id, step_id))
            if len(df) == 0:
                return {"error": "Step not found"}
                
            # 2. Build features X
            refs = sorted({r for fs in df.retrieved_facts for r in (fs or []) if isinstance(r, str)})
            lookup = {}
            if refs:
                cur = conn.cursor()
                try:
                    cur.execute("select fact_ref, embedding::text from memory_embeddings where fact_ref = any(%s)", (refs,))
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
