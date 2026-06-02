"""
ontology_manifold_probe.py
===========================
Navigation probe for GRAFOMEM `orchestrator_steps`: build one feature vector per
governed agent step, train a hexagonal Self-Organizing Map, and render the
resulting manifold colored by `governance_allowed`.

TWO SEPARATE CLAIMS  (keep them apart when you report results)
--------------------------------------------------------------
(1) ENGINE WORKS      The pipeline vectorizes, trains, places, and renders a
                      navigable map, AND surfaces latent structure WHEN it
                      exists (and only then). Fully provable on synthetic data.
(2) REAL CLUSTERING   Whether *real* governed steps form meaningful, useful
                      neighborhoods. NOT provable on synthetic data — the
                      synthetic generator decides its own structure. Requires
                      the live corpus once the dogfood fills the table.

The synthetic generator below has a `mode`:
    mode="structured"  latent topics drive facts + text + outcome  -> tests (1)+
    mode="null"        facts/outcome decoupled from topics          -> the floor
Comparing the two is how you show the engine reflects signal, not noise — the
only honest version of claim (1) on synthetic data.

Feature vector follows the Part-3 / Q4 encodings exactly:
    model_id, agent_role, workflow_id      single, low card   -> one-hot
    tool_calls[], policy_name[]            multi,  low card   -> multi-hot
    retrieved_facts[]                      multi,  HIGH card   -> feature hashing
                                           (the entity-sharing proximity signal)
    tokens_used, latency_ms, step_number   continuous          -> standardized
    input_text + raw_output                free text           -> TF-IDF -> SVD
Each block is L2-normalized then scaled by a weight (facts weighted highest).

Swap synthetic -> live with  --source db --dsn <dsn>  once rows exist; the
extraction query is in load_from_db().
"""

from __future__ import annotations
import argparse, hashlib, random
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------- 
# 1. Synthetic corpus  (schema-mirrored; columns match the extraction query)
# -----------------------------------------------------------------------------
AGENT_ROLES = ["planner", "retriever", "critic", "executor"]          # ~4 roles
WORKFLOWS   = ["sprint_planning", "code_review", "deployment_check"]
MODELS      = ["mock-model", "opus-4", "sonnet-4", "haiku-4"]          # mock + 3
TOOLS       = ["search", "read_file", "write_file", "exec", "http", "vector_lookup"]
POLICIES    = ["pii_guard", "budget_cap", "tool_allowlist", "rate_limit",
               "egress_block", "memory_scope", "escalation"]
FACT_VOCAB  = [f"fact_{i:04d}" for i in range(400)]                    # high card

@dataclass
class Topic:
    """A latent decision archetype: its own fact cluster, tool/policy bias, etc."""
    name: str
    facts: list
    tools: list
    policies: list
    agent_bias: str
    workflow: str
    deny_rate: float
    query_stub: str

def _make_topics(k: int, rng: random.Random) -> list[Topic]:
    topics = []
    chunk = len(FACT_VOCAB) // k
    stubs = ["plan the sprint backlog", "review the diff for regressions",
             "check deploy readiness", "summarize retrieved context",
             "validate the tool output", "assess governance risk"]
    for i in range(k):
        topics.append(Topic(
            name=f"topic_{i}",
            facts=FACT_VOCAB[i * chunk:(i + 1) * chunk],         # owns a fact cluster
            tools=rng.sample(TOOLS, k=rng.randint(2, 3)),
            policies=rng.sample(POLICIES, k=rng.randint(1, 3)),
            agent_bias=rng.choice(AGENT_ROLES),
            workflow=rng.choice(WORKFLOWS),
            deny_rate=rng.choice([0.05, 0.10, 0.30, 0.45]),
            query_stub=stubs[i % len(stubs)],
        ))
    return topics

def synth_steps(n: int, k_topics: int = 6, mode: str = "structured",
                seed: int = 7) -> pd.DataFrame:
    rng = random.Random(seed)
    topics = _make_topics(k_topics, rng)
    rows = []
    sess_idx, sess_left, prev_step = -1, 0, None
    for s in range(n):
        step_id = f"step_{s:05d}"
        if sess_left <= 0:                          # start a new session chain
            sess_idx += 1
            sess_left = rng.randint(1, 5)
            prev_step = None
        if mode == "null":
            # TRUE null: every field i.i.d. random, decoupled from any topic.
            facts = rng.sample(FACT_VOCAB, k=rng.randint(3, 8))
            tools = rng.sample(TOOLS, k=rng.randint(1, 3))
            gov_logs = [{"policy_name": p, "result": rng.choice(["allowed", "denied"])}
                        for p in rng.sample(POLICIES, k=rng.randint(1, 3))]
            denied = rng.random() < 0.2
            row = dict(
                agent_role=rng.choice(AGENT_ROLES),
                workflow_id=rng.choice(WORKFLOWS),
                input_text=f"step over {' '.join(facts[:2])}",
                raw_output=("blocked by policy" if denied else "completed step"),
                _topic=rng.choice(topics).name,        # label decorrelated from features
            )
        else:
            t = rng.choice(topics)
            kf = rng.randint(3, 8)
            facts = rng.sample(t.facts, k=min(kf, len(t.facts)))    # from topic's cluster
            if rng.random() < 0.25:
                facts.append(rng.choice(FACT_VOCAB))               # noise fact
            tools = list({*rng.sample(t.tools, k=rng.randint(1, len(t.tools))),
                          *([rng.choice(TOOLS)] if rng.random() < 0.2 else [])})
            gov_logs, denied = [], False
            for p in t.policies:                                   # denied policy -> not allowed
                res = "denied" if (rng.random() < t.deny_rate) else "allowed"
                denied = denied or (res == "denied")
                gov_logs.append({"policy_name": p, "result": res})
            row = dict(
                agent_role=t.agent_bias if rng.random() < 0.8 else rng.choice(AGENT_ROLES),
                workflow_id=t.workflow,
                input_text=f"{t.query_stub} using {' '.join(facts[:2])}",
                raw_output=("blocked by policy" if denied else f"completed {t.query_stub}"),
                _topic=t.name,                                     # ground truth (synthetic only)
            )
        row.update(dict(
            step_id=step_id,
            session_id=f"sess_{sess_idx:04d}",
            parent_decision_id=prev_step,
            model_id=rng.choice(MODELS),
            governance_allowed=(not denied),
            tool_calls=tools,
            governance_logs=gov_logs,
            retrieved_facts=facts,
            tokens_used=int(rng.gauss(800, 250) + len(facts) * 40),
            latency_ms=int(abs(rng.gauss(1200, 600)) + len(tools) * 80),
            step_number=rng.randint(1, 12),
            created_at=pd.Timestamp("2026-05-01") + pd.Timedelta(minutes=s * 3),
        ))
        rows.append(row)
        prev_step, sess_left = step_id, sess_left - 1
    return pd.DataFrame(rows)

# ----------------------------------------------------------------------------- 
# 2. Feature vector  (Q4 encodings; per-block L2-normalize then weight)
# -----------------------------------------------------------------------------
@dataclass
class Weights:
    facts: float = 3.0      # the proximity signal — highest weight
    text:  float = 1.5
    multi: float = 1.0      # tools + policies
    cat:   float = 0.7      # model / agent / workflow
    num:   float = 0.5

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
        for v in items:
            if v in idx:
                M[r, idx[v]] = 1.0
    return M

def _hash_facts(lists, dim=64):
    """Signed feature hashing of high-cardinality fact-id sets (deterministic)."""
    M = np.zeros((len(lists), dim))
    for r, facts in enumerate(lists):
        for f in facts:
            h = int(hashlib.md5(f.encode()).hexdigest(), 16)
            M[r, h % dim] += 1.0 if (h >> 8) & 1 else -1.0
    return M

def _l2(M):
    n = np.linalg.norm(M, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return M / n

def build_features(df: pd.DataFrame, w: Weights = Weights(),
                   facts_dim=64, text_dim=16, about=None):
    """about=None -> SYNTHETIC path (ID-overlap hashing + TF-IDF intent proxy).
    about=(n x 384) -> REAL path: the bge 'about' vector replaces BOTH the
    hashed-fact block and the TF-IDF block (text is already folded into it)."""
    from sklearn.preprocessing import StandardScaler

    cat = np.hstack([_onehot(df.model_id, MODELS),
                     _onehot(df.agent_role, AGENT_ROLES),
                     _onehot(df.workflow_id, WORKFLOWS)])
    policy_lists = [[g["policy_name"] for g in gl] for gl in df.governance_logs]
    multi = np.hstack([_multihot(df.tool_calls, TOOLS),
                       _multihot(policy_lists, POLICIES)])
    num = StandardScaler().fit_transform(
        df[["tokens_used", "latency_ms", "step_number"]].to_numpy(float))

    if about is not None:                                   # REAL: pooled bge vectors
        about = np.asarray(about, float)
        blocks = [(_l2(about), w.facts), (_l2(multi), w.multi),
                  (_l2(cat), w.cat), (_l2(num), w.num)]
        dims = {"about(bge384)": about.shape[1], "multi": multi.shape[1],
                "cat": cat.shape[1], "num": num.shape[1]}
    else:                                                   # SYNTHETIC: honest proxy
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.decomposition import TruncatedSVD
        facts = _hash_facts(df.retrieved_facts, dim=facts_dim)
        corpus = (df.input_text.fillna("") + " " + df.raw_output.fillna("")).tolist()
        tfidf = TfidfVectorizer(max_features=512).fit_transform(corpus)
        svd_k = max(2, min(text_dim, tfidf.shape[1] - 1, tfidf.shape[0] - 1))
        text = TruncatedSVD(n_components=svd_k, random_state=0).fit_transform(tfidf)
        blocks = [(_l2(facts), w.facts), (_l2(text), w.text),
                  (_l2(multi), w.multi), (_l2(cat), w.cat), (_l2(num), w.num)]
        dims = {"facts(hash)": facts.shape[1], "text(tfidf)": text.shape[1],
                "multi": multi.shape[1], "cat": cat.shape[1], "num": num.shape[1]}

    X = np.hstack([B * np.sqrt(wt) for B, wt in blocks])
    return X.astype(float), dims


# --- Real-data "about" vector: pooled bge-small-en fact embeddings + text fallback ---
EMB_DIM   = 384                        # BAAI/bge-small-en-v1.5
BGE_MODEL = "BAAI/bge-small-en-v1.5"

def make_about_vectors(df, fact_vec_lookup, model=None, text_weight=0.3):
    """Per step: score-weighted mean of its retrieved facts' 384-d bge vectors,
    renormalized (bge is a cosine space), blended with a small contribution from
    a bge embedding of (input_text + raw_output). Steps with empty
    retrieved_facts fall back to pure step-text. Returns n x 384, rows unit-norm.

    fact_vec_lookup: {fact_ref -> np.ndarray(384)} fetched from pgvector.
    IMPORTANT: embed the step text with the SAME recipe as the stored fact
    *passages* (bge: no query-instruction prefix on passages) so both vectors
    live in one regime; otherwise no-retrieval steps land in a shifted space.
    """
    if model is None:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(BGE_MODEL)
    texts = (df.input_text.fillna("") + " " + df.raw_output.fillna("")).tolist()
    text_emb = np.asarray(model.encode(texts, normalize_embeddings=True))
    scores_col = df["retrieval_scores"] if "retrieval_scores" in df else [None] * len(df)
    out = np.zeros((len(df), EMB_DIM))
    for i, (facts, scores) in enumerate(zip(df.retrieved_facts, scores_col)):
        vecs = [fact_vec_lookup[f] for f in (facts or []) if f in fact_vec_lookup]
        if vecs:
            V = np.vstack(vecs)
            wts = (np.asarray(scores[:len(vecs)], float) if scores else np.ones(len(vecs)))
            pooled = (V * wts[:, None]).sum(0) / max(wts.sum(), 1e-9)
            pooled /= (np.linalg.norm(pooled) + 1e-9)       # renormalize after pooling
            v = pooled + text_weight * text_emb[i]          # small text contribution
        else:
            v = text_emb[i]                                 # pure text fallback
        out[i] = v / (np.linalg.norm(v) + 1e-9)
    return out

# ----------------------------------------------------------------------------- 
# 3. Hexagonal SOM
# -----------------------------------------------------------------------------
def train_som(X, seed=0):
    from minisom import MiniSom
    n = X.shape[0]
    side = max(6, int(round(np.sqrt(5 * np.sqrt(n)))))           # heuristic grid size
    som = MiniSom(side, side, X.shape[1], topology="hexagonal",
                  activation_distance="euclidean", neighborhood_function="gaussian",
                  sigma=max(1.0, side / 4), learning_rate=0.5, random_seed=seed)
    som.pca_weights_init(X)
    som.train_batch(X, num_iteration=max(2000, n * 5), verbose=False)
    bmu = np.array([som.winner(x) for x in X])                  # (col, row) per step
    return som, side, bmu

def topic_recoverability(df, bmu):
    """How well the latent topic is recovered from 2D grid position (synthetic only).
    kNN on hex-pixel coords, 5-fold CV, vs the chance floor 1/k. Robust to cell
    size, unlike raw per-cell purity. A big gap over chance == the engine
    preserved real structure; collapse to chance == it found none."""
    from sklearn.neighbors import KNeighborsClassifier
    from sklearn.model_selection import cross_val_score
    x = bmu[:, 0] + 0.5 * (bmu[:, 1] % 2)
    y = bmu[:, 1] * (np.sqrt(3) / 2)
    P = np.c_[x, y]
    yt = df["_topic"].to_numpy()
    k = max(3, min(15, len(df) // 20))
    acc = cross_val_score(KNeighborsClassifier(n_neighbors=k), P, yt, cv=5).mean()
    return float(acc), 1.0 / df["_topic"].nunique()

# ----------------------------------------------------------------------------- 
# 4. Render  (compliance lens + topology check)
# -----------------------------------------------------------------------------
def render(df, bmu, side, som, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import RegularPolygon
    from matplotlib import cm
    import matplotlib.colors as mcolors

    tmp = df.copy()
    tmp["cx"], tmp["cy"] = bmu[:, 0], bmu[:, 1]
    agg = tmp.groupby(["cx", "cy"]).agg(
        n=("step_id", "size"),
        allow_rate=("governance_allowed", "mean"),
        topic=("_topic", lambda s: s.value_counts().index[0]),
    ).reset_index()

    topics = sorted(df["_topic"].unique())
    tcolor = {t: cm.tab10(i % 10) for i, t in enumerate(topics)}
    # hex pixel positions: odd rows offset by 0.5, rows scaled by sqrt(3)/2
    def xy(cx, cy):
        return cx + 0.5 * (cy % 2), cy * (np.sqrt(3) / 2)

    fig, axes = plt.subplots(1, 2, figsize=(15, 7))
    rdgn = plt.get_cmap("RdYlGn")
    for ax, mode in zip(axes, ["compliance", "topic"]):
        for _, r in agg.iterrows():
            x, y = xy(r.cx, r.cy)
            if mode == "compliance":
                fc = rdgn(r.allow_rate)
            else:
                fc = tcolor[r.topic]
            alpha = 0.35 + 0.65 * min(1.0, r.n / agg.n.quantile(0.9))
            ax.add_patch(RegularPolygon((x, y), numVertices=6, radius=0.58,
                                        orientation=np.pi / 6, facecolor=fc,
                                        edgecolor="white", lw=0.5, alpha=alpha))
        ax.set_xlim(-1, side + 1); ax.set_ylim(-1, side * np.sqrt(3) / 2 + 1)
        ax.set_aspect("equal"); ax.axis("off")
    axes[0].set_title("Compliance lens  (green = allowed, red = denied)", fontsize=12)
    axes[1].set_title("Latent topic  (synthetic ground truth — engine check only)",
                      fontsize=12)
    fig.suptitle("Ontology Manifold probe — hex SOM over synthetic orchestrator_steps",
                 fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(path, dpi=130, bbox_inches="tight")
    print(f"[render] wrote {path}")

# ----------------------------------------------------------------------------- 
# 5. Live data swap-in  (use once orchestrator_steps has rows)
# -----------------------------------------------------------------------------
EXTRACTION_SQL = """
select s.step_id, a.role agent_role, s.workflow_id, s.model_id, s.governance_allowed,
       s.tool_calls, s.governance_logs, s.retrieved_facts,
       s.tokens_used, s.latency_ms, s.step_number, s.created_at,
       s.input_text, s.raw_output
from orchestrator_steps s
left join orchestrator_agents a on a.agent_id = s.agent_id
order by s.created_at;
"""

def _parse_pgvector(v):
    if isinstance(v, (list, tuple, np.ndarray)):
        return list(v)
    return [float(x) for x in str(v).strip("[]").split(",")]

def load_from_db(dsn, emb_table="memory_embeddings", emb_col="embedding", emb_key="fact_ref"):
    """Load real steps and build the bge 'about' vector per step.
    CONFIRM the embedding location before trusting the defaults — your live DB
    showed both `memories` and `memory_embeddings`, so check which holds the
    vector(384) column and what key joins to orchestrator_steps.retrieved_facts:
        \\d memory_embeddings    \\d memories
    (pgvector 0.8.2 can also AVG() server-side if you'd rather pool in SQL, but
    Python pooling here lets you score-weight with retrieval_scores.)"""
    import psycopg2
    from sentence_transformers import SentenceTransformer
    conn = psycopg2.connect(dsn)
    try:
        df = pd.read_sql(EXTRACTION_SQL, conn)
        refs = sorted({r for fs in df.retrieved_facts for r in (fs or [])})
        lookup = {}
        if refs:
            cur = conn.cursor()
            cur.execute(f"select {emb_key}, {emb_col}::text from {emb_table} "
                        f"where {emb_key} = any(%s)", (refs,))
            lookup = {k: np.asarray(_parse_pgvector(v), float) for k, v in cur.fetchall()}
    finally:
        conn.close()
    df["_topic"] = "real"   # no synthetic ground truth -> recovery metric disabled
    about = make_about_vectors(df, lookup, SentenceTransformer(BGE_MODEL))
    return df, about

# -----------------------------------------------------------------------------
# 5b. Bridge: REAL bge geometry over INVENTED decisions
# -----------------------------------------------------------------------------
# Validates the engine + UX on a *semantically real* map before the dogfood
# fills orchestrator_steps. The vectors are genuine bge-small-en embeddings; the
# decisions are fabricated. It proves related steps cluster and the UI is
# navigable -- NOT that PRODUCTION steps cluster usefully (that needs live data).
# Exported with source="bridge" so the UI badge says exactly this.

class BgeEmbedder:
    """.encode(texts, normalize_embeddings=True) over BAAI/bge-small-en-v1.5,
    via fastembed (ONNX, light) if available, else sentence-transformers."""
    def __init__(self):
        try:
            from fastembed import TextEmbedding
            self._fe = TextEmbedding(model_name=BGE_MODEL); self.backend = "fastembed"
        except Exception:
            from sentence_transformers import SentenceTransformer
            self._st = SentenceTransformer(BGE_MODEL); self.backend = "sentence-transformers"
    def encode(self, texts, normalize_embeddings=True):
        texts = list(texts)
        if self.backend == "fastembed":
            V = np.asarray(list(self._fe.embed(texts)), float)
        else:
            V = np.asarray(self._st.encode(texts, normalize_embeddings=False), float)
        if normalize_embeddings:
            nrm = np.linalg.norm(V, axis=1, keepdims=True); nrm[nrm == 0] = 1.0; V = V / nrm
        return V

# topic -> (fact sentences, query templates, typical tools, typical policies, deny_rate)
BRIDGE_TOPICS = {
    "pii_handling": (
        ["User email addresses must be redacted before logging.",
         "GDPR Article 17 grants the right to erasure of personal data.",
         "Phone numbers and national IDs are classified as sensitive PII.",
         "Pseudonymized identifiers still count as personal data under GDPR.",
         "PII must never be sent to third-party model providers.",
         "Data subject access requests must be fulfilled within thirty days."],
        ["redact PII from {x}", "check {x} for personal data leaks"],
        ["vector_lookup", "read_file"], ["pii_guard", "memory_scope"], 0.40),
    "deployment": (
        ["Production deploys require a green CI pipeline and two approvals.",
         "Rollback triggers automatically if the error rate exceeds two percent.",
         "Blue-green deployment keeps the previous version warm for a day.",
         "Database migrations run in a separate step before the app deploy.",
         "Canary releases route five percent of traffic to the new build first.",
         "Deploy windows are restricted to business hours in the prod region."],
        ["check deploy readiness for {x}", "verify the rollback plan for {x}"],
        ["exec", "http", "read_file"], ["tool_allowlist", "escalation"], 0.10),
    "sprint_planning": (
        ["Last sprint velocity averaged thirty-two story points.",
         "Carryover tickets should be re-estimated before re-committing.",
         "The backlog is prioritized by reach, impact, confidence, and effort.",
         "Team capacity drops twenty percent during the holiday sprint.",
         "Spikes are time-boxed to two days and produce a written finding.",
         "Each sprint reserves fifteen percent for unplanned support work."],
        ["plan the sprint backlog for {x}", "estimate capacity for {x}"],
        ["read_file", "search"], ["rate_limit"], 0.05),
    "code_review": (
        ["Unparameterized SQL queries are flagged as injection risks.",
         "New endpoints must include authorization checks and tests.",
         "Secrets must never be committed; use the vault reference instead.",
         "N+1 query patterns should be replaced with a single joined query.",
         "Public functions require docstrings and explicit error handling.",
         "Diffs over four hundred lines should be split for reviewability."],
        ["review the diff in {x} for regressions", "audit {x} for security issues"],
        ["read_file", "search", "exec"], ["tool_allowlist"], 0.12),
    "cost_budget": (
        ["The monthly inference budget cap is fifty thousand dollars per tenant.",
         "Requests exceeding the token budget are queued, not dropped.",
         "Opus-tier models cost roughly fifteen times a haiku-tier call.",
         "Rate limits are one hundred requests per minute per API key.",
         "Cost anomalies above three sigma trigger an escalation alert.",
         "Batch endpoints are billed at half the synchronous rate."],
        ["assess the cost of {x}", "check budget headroom for {x}"],
        ["http", "search"], ["budget_cap", "rate_limit"], 0.45),
    "vendor_compliance": (
        ["Vendor SOC 2 reports must be renewed annually.",
         "Sub-processors must be listed in the data processing agreement.",
         "SLA breaches over four hours require a written incident report.",
         "Vendor access is revoked within a day of contract termination.",
         "High-risk vendors undergo a quarterly security review.",
         "Data residency clauses restrict storage to the EU region."],
        ["run a vendor check on {x}", "review the contract terms for {x}"],
        ["read_file", "http"], ["egress_block", "escalation"], 0.20),
}
BRIDGE_SUBJECTS = ["the auth service", "the billing module", "the Q3 release",
                   "the data pipeline", "the mobile client", "the search index"]

def bridge_corpus():
    facts = {}
    for t, (sents, *_rest) in BRIDGE_TOPICS.items():
        for i, s in enumerate(sents):
            facts[f"f_{t}_{i:02d}"] = s
    return facts

def bridge_steps(n, seed=11):
    rng = random.Random(seed)
    by_topic = {t: [f"f_{t}_{i:02d}" for i in range(len(v[0]))]
                for t, v in BRIDGE_TOPICS.items()}
    all_facts = [f for fs in by_topic.values() for f in fs]
    names = list(BRIDGE_TOPICS)
    rows = []
    sess_idx, sess_left, prev = -1, 0, None
    for s in range(n):
        if sess_left <= 0:
            sess_idx += 1; sess_left = rng.randint(1, 5); prev = None
        t = rng.choice(names)
        sents, queries, tools_t, pols_t, deny = BRIDGE_TOPICS[t]
        fids = rng.sample(by_topic[t], k=rng.randint(2, 4))
        if rng.random() < 0.2:
            fids.append(rng.choice(all_facts))                 # cross-topic noise fact
        gov_logs, denied = [], False
        for p in pols_t:
            res = "denied" if rng.random() < deny else "allowed"
            denied = denied or res == "denied"
            gov_logs.append({"policy_name": p, "result": res})
        q = rng.choice(queries).format(x=rng.choice(BRIDGE_SUBJECTS))
        sid = f"step_{s:05d}"
        rows.append(dict(
            step_id=sid, session_id=f"sess_{sess_idx:04d}", parent_decision_id=prev,
            agent_role=rng.choice(AGENT_ROLES), workflow_id=rng.choice(WORKFLOWS),
            model_id=rng.choice(MODELS), governance_allowed=(not denied),
            tool_calls=list({*tools_t, *([rng.choice(TOOLS)] if rng.random() < 0.2 else [])}),
            governance_logs=gov_logs, retrieved_facts=fids,
            tokens_used=int(rng.gauss(900, 250) + len(fids) * 40),
            latency_ms=int(abs(rng.gauss(1300, 600)) + len(tools_t) * 80),
            step_number=rng.randint(1, 12),
            created_at=pd.Timestamp("2026-05-01") + pd.Timedelta(minutes=s * 3),
            input_text=q,
            raw_output=("blocked by policy" if denied else f"completed: {q}"),
            _topic=t,
        ))
        prev, sess_left = sid, sess_left - 1
    return pd.DataFrame(rows)

def bridge_dataset(n):
    emb = BgeEmbedder()
    facts = bridge_corpus()
    fids = list(facts)
    vecs = emb.encode([facts[f] for f in fids], normalize_embeddings=True)
    fact_lookup = {f: v for f, v in zip(fids, vecs)}
    df = bridge_steps(n)
    about = make_about_vectors(df, fact_lookup, model=emb)
    print(f"[bridge] bge backend = {emb.backend}; embedded {len(fids)} fact texts "
          f"+ {len(df)} step texts")
    return df, about

# ----------------------------------------------------------------------------- 
# 6. Driver
# -----------------------------------------------------------------------------
def export_manifold(df, bmu, side, path, source="synthetic", hex_px=40.0):
    """Serialize the trained map into the cloud-v2 `manifold.json` contract
    (see cloud-v2-manifold-uix-spec.md §3). Synthetic data exposes only the
    honestly-derivable lenses (compliance, latency); erasure/integrity lenses
    arrive with live audit (gcrumbs) data."""
    import json, datetime as dt
    LENSES = ["compliance", "latency"]
    d = df.reset_index(drop=True).copy()
    d["_q"], d["_r"] = bmu[:, 0], bmu[:, 1]
    d["_cell"] = [f"c_{int(a):02d}_{int(b):02d}" for a, b in zip(d["_q"], d["_r"])]

    cells = []
    for (cq, cr), g in d.groupby(["_q", "_r"]):
        pols = [p["policy_name"] for gl in g.governance_logs for p in gl]
        top_pol = pd.Series(pols).value_counts().index[0] if pols else "—"
        x = (cq + 0.5 * (cr % 2)) * hex_px
        y = cr * (np.sqrt(3) / 2) * hex_px
        cells.append(dict(
            id=f"c_{int(cq):02d}_{int(cr):02d}", q=int(cq), r=int(cr),
            x=round(float(x), 1), y=round(float(y), 1), count=int(len(g)),
            label=f"agent:{g.agent_role.value_counts().index[0]} · policy:{top_pol}",
            exemplars=g.step_id.head(8).tolist(),
            lenses=dict(compliance=round(float(g.governance_allowed.mean()), 3),
                        latency=round(float(g.latency_ms.mean()), 1)),
        ))

    steps = [dict(stepId=sid, cellId=cid, governanceAllowed=bool(ga),
                  agentRole=ar, workflowId=wf, modelId=mid,
                  createdAt=pd.Timestamp(ts).isoformat())
             for sid, cid, ga, ar, wf, mid, ts in zip(
                 d["step_id"], d["_cell"], d["governance_allowed"], d["agent_role"],
                 d["workflow_id"], d["model_id"], d["created_at"])]

    edges = []
    if "parent_decision_id" in d:
        for sid, p in zip(d["step_id"], d["parent_decision_id"]):
            if isinstance(p, str):          # excludes None / NaN at session starts
                edges.append({"from": p, "to": sid, "kind": "parent"})

    manifold = dict(
        meta=dict(version="0.1.0", generatedAt=dt.datetime.utcnow().isoformat() + "Z",
                  source=source, somGrid=[int(side), int(side)], nSteps=int(len(d)),
                  lenses=LENSES),
        cells=cells, steps=steps, edges=edges)
    with open(path, "w") as f:
        json.dump(manifold, f, indent=2)
    print(f"[export] wrote {path}  (cells={len(cells)} steps={len(steps)} "
          f"edges={len(edges)} source={source})")

def run(df, tag, out_png=None, about=None, export_path=None, source="synthetic"):
    X, dims = build_features(df, about=about)
    som, side, bmu = train_som(X)
    if df["_topic"].nunique() >= 2:
        acc, chance = topic_recoverability(df, bmu)
        metric = f"topic_recovery={acc:.3f}  (chance={chance:.3f})"
    else:
        acc, chance = float("nan"), float("nan")
        metric = "topic_recovery=n/a (live data has no synthetic ground truth)"
    print(f"[{tag}] n={len(df)}  feature_dim={X.shape[1]}  blocks={dims}  "
          f"grid={side}x{side}  {metric}")
    if out_png:
        render(df, bmu, side, som, out_png)
    if export_path:
        export_manifold(df, bmu, side, export_path, source=source)
    return acc, chance

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["synthetic", "bridge", "db"], default="synthetic")
    ap.add_argument("--dsn", default="postgresql://grafomem:grafomem@localhost:5433/grafomem")
    ap.add_argument("--n", type=int, default=1500)
    ap.add_argument("--out", default="manifold_probe.png")
    ap.add_argument("--export", default=None,
                    help="write manifold.json (cloud-v2 contract) from the trained map")
    ap.add_argument("--smoke-real", action="store_true",
                    help="plumbing test of the 384-d embedding path with FABRICATED "
                         "vectors — proves the code runs; claims nothing about clustering")
    args = ap.parse_args()

    if args.source == "db":
        df, about = load_from_db(args.dsn)            # real bge vectors via pgvector
        run(df, "live", args.out, about=about, export_path=args.export, source="live")
        return

    if args.source == "bridge":
        df, about = bridge_dataset(args.n)            # real bge vectors, invented decisions
        run(df, "bridge", args.out, about=about, export_path=args.export, source="bridge")
        print("Bridge: REAL bge geometry over INVENTED decisions. Validates the engine "
              "and UX (semantically related steps cluster, the map is navigable) — NOT "
              "that production steps cluster usefully. The artifact's source='bridge' "
              "carries that caveat into the UI.")
        return

    if args.smoke_real:
        df = synth_steps(args.n, mode="null")
        about = np.random.default_rng(0).normal(size=(len(df), EMB_DIM))
        run(df, "smoke-real(fabricated 384-d)", about=about)
        print("Plumbing only: the 384-d 'about' path executed end-to-end, and "
              "recovery sits at chance as it must (fabricated vectors carry no "
              "signal). Real bge vectors come from the DB.")
        return

    print("=== Claim-separation run: structured vs TRUE null synthetic ===")
    acc_s, chance = run(synth_steps(args.n, mode="structured"), "structured", args.out,
                        export_path=args.export, source="synthetic")
    acc_n, _      = run(synth_steps(args.n, mode="null"), "null")
    print(f"\nENGINE-WORKS evidence: topic recovery {acc_s:.3f} (structured) vs "
          f"{acc_n:.3f} (true null); chance = {chance:.3f}.")
    print("A large structured-vs-null gap means the engine surfaces structure when "
          "it exists and collapses toward chance when it doesn't. This says NOTHING "
          "about whether REAL GRAFOMEM steps cluster usefully — run --source db on "
          "the live corpus for claim (2).")

if __name__ == "__main__":
    main()
