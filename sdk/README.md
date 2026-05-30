# GRAFOMEM Python SDK

**The only AI agent platform where every step is governed, every decision is signed, and every action is replayable.**

[![PyPI](https://img.shields.io/pypi/v/grafomem)](https://pypi.org/project/grafomem/)
[![Python](https://img.shields.io/pypi/pyversions/grafomem)](https://pypi.org/project/grafomem/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Installation

```bash
pip install grafomem
```

With LangChain support:

```bash
pip install 'grafomem[langchain]'
```

## Quick Start

```python
from grafomem import GrafomemClient

client = GrafomemClient(
    api_key="gfm_...",
    base_url="https://cloud.grafomem.com",  # or http://localhost:8080
)

# Create a memory store
store = client.stores.create(name="my-agent-memory")

# Write and retrieve memories
client.memories.write(store.id, text="User prefers dark mode", source="onboarding")
results = client.memories.retrieve(store.id, query="user preferences", top_k=5)
for memory in results:
    print(f"  [{memory.score:.2f}] {memory.text}")
```

## Services

| Service | Access | What It Does |
|---|---|---|
| **stores** | `client.stores.*` | Create, list, flush memory stores |
| **memories** | `client.memories.*` | Write, retrieve, delete, supersede memories |
| **governance** | `client.governance.*` | Policy CRUD, evaluation, audit logs |
| **orchestrator** | `client.orchestrator.*` | Agents, workflows, receipts, replay |
| **decisions** | `client.decisions.*` | Decision trail (signed inference log) |
| **erasure** | `client.erasure.*` | GDPR erasure certificates |
| **reports** | `client.reports.*` | EU AI Act / GDPR / DORA compliance |
| **llm** | `client.llm.*` | LLM providers and tools (BYOM) |
| **portal** | `client.portal.*` | Signup and login |

## Governed Agent Orchestration

```python
# Register your LLM
client.llm.register_provider(
    model_id="gpt-4o-mini", provider="openai", api_key="sk-..."
)

# Define agents
researcher = client.orchestrator.create_agent(
    name="researcher", role="researcher", model_id="gpt-4o-mini",
    system_prompt="You are a research analyst.", store_id=store.id,
)
writer = client.orchestrator.create_agent(
    name="writer", role="writer", model_id="gpt-4o-mini",
    system_prompt="You are a technical writer.",
)

# Create and run a workflow
workflow = client.orchestrator.create_workflow(
    name="research-report", agents=[researcher.id, writer.id],
)
run = client.orchestrator.run_workflow(workflow.id, input="Analyze Q2 results")
print(f"Status: {run.status}, Tokens: {run.total_tokens}")

# Verify the execution chain
chain = client.orchestrator.verify_chain(workflow.id)
print(f"Chain integrity: {chain.status}")  # "intact"
```

## Governance & Compliance

```python
# Create a PII guard policy
client.governance.create_policy(
    name="PII Guard",
    policy_type="pii_guard",
    action="deny",
    config={"patterns": [r"\b\d{3}-\d{2}-\d{4}\b"]},
)

# Evaluate before sending to LLM
result = client.governance.evaluate(
    operation="output_check",
    context={"output": "Contact John, SSN 123-45-6789"},
)
if not result.allowed:
    print("Blocked! PII detected.")

# Generate compliance report
report = client.reports.generate(framework="eu_ai_act")
for section in report.sections:
    print(f"  {section.article}: {section.status}")
```

## GDPR Erasure

```python
# Issue a signed erasure certificate
cert = client.erasure.issue(
    store_id=store.id, fact_ref="42", reason="GDPR Art 17"
)

# Verify independently
result = client.erasure.verify(cert.id)
assert result.valid  # Ed25519 signature verified
```

## LangChain Integration

```python
from grafomem.langchain import GrafomemMemory
from langchain.chains import ConversationChain
from langchain_openai import ChatOpenAI

memory = GrafomemMemory(client=client, store_id=store.id, top_k=10)
chain = ConversationChain(llm=ChatOpenAI(), memory=memory)

chain.invoke({"input": "What are the latest quarterly results?"})
# Memory is automatically saved to GRAFOMEM with full governance
```

### LangGraph / LCEL

```python
from grafomem.langchain import GrafomemChatMessageHistory
from langchain_core.runnables.history import RunnableWithMessageHistory

def get_history(session_id: str):
    return GrafomemChatMessageHistory(
        client=client, store_id=store.id, session_id=session_id,
    )

chain_with_history = RunnableWithMessageHistory(
    chain, get_history, input_messages_key="input",
)
```

## Error Handling

```python
from grafomem import GrafomemClient, AuthenticationError, NotFoundError, RateLimitError

try:
    client.memories.retrieve("nonexistent-store", query="test")
except NotFoundError:
    print("Store not found")
except AuthenticationError:
    print("Invalid API key")
except RateLimitError:
    print("Too many requests — will auto-retry")
```

## Links

- **Documentation**: [docs.grafomem.com/sdk](https://docs.grafomem.com/sdk)
- **API Reference**: [cloud.grafomem.com/docs](https://cloud.grafomem.com/docs)
- **Portal**: [cloud.grafomem.com/portal](https://cloud.grafomem.com/portal)
- **GitHub**: [github.com/GNS-Foundation/grafomem](https://github.com/GNS-Foundation/grafomem)

## License

MIT — see [LICENSE](../LICENSE).
