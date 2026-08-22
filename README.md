# GRAFOMEM

**The governed memory runtime for agents.** Signed checkpoints, provable erasure, portable memory — a drop-in wrapper for your LangGraph checkpointer.

[![PyPI](https://img.shields.io/pypi/v/grafomem)](https://pypi.org/project/grafomem/)
[![License: MIT](https://img.shields.io/pypi/l/grafomem)](LICENSE)
[![CI](https://github.com/GNS-Foundation/grafomem/actions/workflows/ci.yml/badge.svg)](https://github.com/GNS-Foundation/grafomem/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/grafomem)](https://pypi.org/project/grafomem/)

```bash
pip install grafomem langgraph-checkpoint-grafomem langgraph
```

```python
from typing import TypedDict
from cryptography.hazmat.primitives.asymmetric import ed25519
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from grafomem_checkpoint import GrafomemSerializer, GrafomemCheckpointSaver

# ── the entire GRAFOMEM integration: an Ed25519 signing key, then wrap ANY
#    LangGraph checkpointer. Pass it to compile() as you already do. ──
priv = ed25519.Ed25519PrivateKey.generate()
saver = GrafomemCheckpointSaver(MemorySaver(serde=GrafomemSerializer(private_key=priv)))

# ── your ordinary LangGraph agent ──
class State(TypedDict):
    messages: list

def agent(state: State) -> State:
    return {"messages": state["messages"] + ["hello from the agent"]}

b = StateGraph(State); b.add_node("agent", agent)
b.add_edge(START, "agent"); b.add_edge("agent", END)
app = b.compile(checkpointer=saver)

cfg = {"configurable": {"thread_id": "user-42"}}
app.invoke({"messages": []}, cfg)

# signed, content-addressed checkpoint
tup = saver.get_tuple(cfg)
print("signed checkpoint hash:", tup.metadata["grafomem_content_hash"])

# cryptographic erasure receipt — proof the erasure transition occurred
saver.delete_thread("user-42")
print("erasure receipt:", saver.last_receipt("user-42"))
```

```text
signed checkpoint hash: ecd0e28938738cc55b3c888f7449503fd586723a699e1d326d74cc0f154874f7
erasure receipt: LangGraphErasureReceipt(pre_state_hash='d9a16ef8…', post_state_hash='0e5751c0…',
                 scope='user-42', key_id='grafomem_checkpoint', timestamp='2026-…', signature=b'…')
```
*(hashes and signature vary per run — each run generates a fresh key)*

**What just happened:** every state transition your agent made was captured as a signed, content-addressed checkpoint — and when you deleted, you got a cryptographic receipt proving the erasure transition occurred. Memory your agents can move, merge, and prove they erased.

## Why

Agent memory today is a JSON blob you have to trust. GRAFOMEM makes it evidence: every write signed, every fact content-addressed, every deletion receipted. When someone asks *"what did your agent know, and when?"* — you answer with proofs, not logs.

## Two tiers, one system

- **Working memory** — fast, bounded context state for the agent loop.
- **Durable facts (GMP)** — governed, bi-temporal, signed facts with provenance. The GRAFOMEM Memory Protocol is an open spec with an executable conformance suite: a backend's capability counts as *supported* when it passes the test, not when the vendor says so.

→ [Architecture overview](https://docs.grafomem.com/architecture/overview)

## Integrations

- **LangGraph** — the quickstart above. → [docs](https://docs.grafomem.com/integrations/langchain)
- **Claude / MCP** — expose governed memory as MCP tools. → [docs](https://docs.grafomem.com/integrations/claude-mcp)
- **Reference server** — a REST + MCP server (`grafomem[server]` extra); a hosted instance runs live at [api.grafomem.com](https://api.grafomem.com/healthz). → [self-hosting docs](https://docs.grafomem.com/server/self-hosting)

## The bigger picture: verify the agent, not just the answer

Governed memory is the evidence substrate for something larger: **Capability-Grounded Reputation (CGR)** — reputation an agent earns per domain from judgments that later resolve against real outcomes, with peer reviews weighted by the reviewer's own demonstrated calibration. Score and evidence mass travel together; fresh identities don't arrive with influence. The scoring model is documented and independently reproducible — [cgr-bench](https://github.com/GNS-Foundation/cgr-bench) reproduces its properties from source: cold-start and Sybil-resistance behavior asserted in CI, an early-warning signal of −0.997 against real credit-default outcomes at 25% resolution, and reviewer calibration that beats a naive equal-weight crowd by ~14% out-of-sample on ~1,900 real human forecasters (held-out reliability recovery r ≈ 0.5–0.65 across split designs). Reputation as evidence, not assertion. → [CGR overview](https://docs.grafomem.com/cgr/overview)

## License

Runtime: **MIT**. The GMP spec is open. → [LICENSE](LICENSE)

---

Docs: [docs.grafomem.com](https://docs.grafomem.com) · Hosted: [cloud.grafomem.com](https://cloud.grafomem.com) (free tier: 10,000 governed decisions / mo) · Issues & discussions welcome.
