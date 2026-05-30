"""LangChain memory adapter for GRAFOMEM Cloud.

Implements :class:`langchain_core.memory.BaseMemory` so GRAFOMEM
can be used as the memory backend for any LangChain chain or agent.

Usage::

    from grafomem import GrafomemClient
    from grafomem.langchain import GrafomemMemory
    from langchain.chains import ConversationChain
    from langchain_openai import ChatOpenAI

    client = GrafomemClient(api_key="gfm_...", base_url="http://localhost:8080")
    memory = GrafomemMemory(client=client, store_id="abc123")

    chain = ConversationChain(llm=ChatOpenAI(), memory=memory)
    chain.invoke({"input": "Hello!"})
"""

from __future__ import annotations

from typing import Any

try:
    from langchain_core.memory import BaseMemory
except ImportError:
    raise ImportError(
        "langchain-core is required for the LangChain adapter. "
        "Install it with: pip install 'grafomem[langchain]'"
    )

from grafomem.client import GrafomemClient


class GrafomemMemory(BaseMemory):
    """LangChain memory backed by GRAFOMEM Cloud.

    Each call to :meth:`save_context` writes the conversation turn
    to the GRAFOMEM store. Each call to :meth:`load_memory_variables`
    retrieves the most relevant memories via semantic search.

    Args:
        client: A :class:`GrafomemClient` instance.
        store_id: The memory store ID to use.
        memory_key: The key under which memories are returned
            (default ``"history"``).
        input_key: Key for the human input in the chain inputs.
        output_key: Key for the AI output in the chain outputs.
        top_k: Number of memories to retrieve (default 5).
        source: Source tag for written memories (default ``"langchain"``).

    Example::

        memory = GrafomemMemory(
            client=GrafomemClient(api_key="gfm_..."),
            store_id="my-store",
            top_k=10,
        )
        # Returns {"history": "relevant memories..."}
        memory.load_memory_variables({"input": "What are my preferences?"})
    """

    # Pydantic v2 fields
    client: Any  # GrafomemClient — typed as Any for Pydantic compat
    store_id: str
    memory_key: str = "history"
    input_key: str = "input"
    output_key: str = "output"
    top_k: int = 5
    source: str = "langchain"

    model_config = {"arbitrary_types_allowed": True}

    @property
    def memory_variables(self) -> list[str]:
        """The list of keys this memory provides to the chain."""
        return [self.memory_key]

    def load_memory_variables(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Retrieve relevant memories for the current input.

        Performs semantic search against the GRAFOMEM store using the
        input text as the query.

        Args:
            inputs: Chain inputs containing the ``input_key``.

        Returns:
            Dict with ``memory_key`` mapped to a newline-joined string
            of relevant memories.
        """
        query = inputs.get(self.input_key, "")
        if not query:
            return {self.memory_key: ""}

        results = self.client.memories.retrieve(
            self.store_id,
            query=str(query),
            top_k=self.top_k,
        )

        memory_text = "\n".join(r.content for r in results if r.content)
        return {self.memory_key: memory_text}

    def save_context(
        self,
        inputs: dict[str, Any],
        outputs: dict[str, str],
    ) -> None:
        """Save a conversation turn to the GRAFOMEM store.

        Writes the human input and AI output as a single memory record.

        Args:
            inputs: Chain inputs (must contain ``input_key``).
            outputs: Chain outputs (must contain ``output_key``).
        """
        input_text = inputs.get(self.input_key, "")
        output_text = outputs.get(self.output_key, "")

        if not input_text and not output_text:
            return

        text = f"Human: {input_text}\nAI: {output_text}"
        self.client.memories.write(
            self.store_id,
            content=text,
            source=self.source,
            meta={"type": "conversation_turn"},
        )

    def clear(self) -> None:
        """Clear all memories from the store."""
        self.client.stores.flush(self.store_id)
