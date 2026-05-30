"""LangChain chat message history adapter for GRAFOMEM Cloud.

Implements :class:`langchain_core.chat_history.BaseChatMessageHistory`
for use with LangGraph, LCEL, and ``RunnableWithMessageHistory``.

Usage::

    from grafomem import GrafomemClient
    from grafomem.langchain import GrafomemChatMessageHistory

    client = GrafomemClient(api_key="gfm_...", base_url="http://localhost:8080")
    history = GrafomemChatMessageHistory(
        client=client,
        store_id="abc123",
        session_id="user-42",
    )
    history.add_user_message("Hello!")
    history.add_ai_message("Hi there!")
    print(history.messages)
"""

from __future__ import annotations

from typing import Any

try:
    from langchain_core.chat_history import BaseChatMessageHistory
    from langchain_core.messages import (
        AIMessage,
        BaseMessage,
        HumanMessage,
        SystemMessage,
    )
except ImportError:
    raise ImportError(
        "langchain-core is required for the LangChain adapter. "
        "Install it with: pip install 'grafomem[langchain]'"
    )

from grafomem.client import GrafomemClient


class GrafomemChatMessageHistory(BaseChatMessageHistory):
    """Chat message history stored in GRAFOMEM Cloud.

    Each message is written as a separate memory record with metadata
    indicating the role (``human``, ``ai``, ``system``) and session ID.
    Messages are retrieved in chronological order via semantic search
    scoped to the session.

    Args:
        client: A :class:`GrafomemClient` instance.
        store_id: The memory store ID to use.
        session_id: Unique session identifier for scoping messages.
        max_messages: Maximum messages to retrieve (default 50).

    Example::

        from langchain_core.runnables.history import RunnableWithMessageHistory

        def get_history(session_id: str):
            return GrafomemChatMessageHistory(
                client=client, store_id="store-1", session_id=session_id,
            )

        chain_with_history = RunnableWithMessageHistory(
            chain, get_history, input_messages_key="input",
        )
    """

    def __init__(
        self,
        client: GrafomemClient,
        store_id: str,
        session_id: str,
        *,
        max_messages: int = 50,
    ) -> None:
        self.client = client
        self.store_id = store_id
        self.session_id = session_id
        self.max_messages = max_messages

    @property
    def messages(self) -> list[BaseMessage]:
        """Retrieve all messages for this session.

        Uses semantic search with a broad query scoped to the session
        ID, then sorts by ref (chronological order).
        """
        results = self.client.memories.retrieve(
            self.store_id,
            query=f"session:{self.session_id}",
            top_k=self.max_messages,
        )

        # Filter to this session and sort chronologically
        session_results = [
            r for r in results
            if r.meta.get("session") == self.session_id
        ]
        session_results.sort(key=lambda r: r.ref)

        messages: list[BaseMessage] = []
        for record in session_results:
            role = record.meta.get("role", "human")
            text = record.text

            if role == "ai":
                messages.append(AIMessage(content=text))
            elif role == "system":
                messages.append(SystemMessage(content=text))
            else:
                messages.append(HumanMessage(content=text))

        return messages

    def add_message(self, message: BaseMessage) -> None:
        """Add a message to the history.

        Args:
            message: A LangChain message (HumanMessage, AIMessage, etc.).
        """
        role = message.type  # "human", "ai", "system"
        self.client.memories.write(
            self.store_id,
            content=message.content,
            source="langchain",
            meta={
                "role": role,
                "session": self.session_id,
                "type": "chat_message",
            },
        )

    def clear(self) -> None:
        """Clear all messages.

        .. warning::
            This flushes the entire store, not just this session.
            Use with caution.
        """
        self.client.stores.flush(self.store_id)
