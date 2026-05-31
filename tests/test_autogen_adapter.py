"""GRAFOMEM AutoGen Adapter Tests — Sprint 18.

DB-free tests verifying AutoGen memory and governance hooks
against a mock GrafomemClient. Does NOT require autogen package.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdk" / "src"))

# AutoGen adapter doesn't import autogen at module level,
# so we can import directly
from grafomem.autogen.memory import GrafomemAutoGenMemory
from grafomem.autogen.hooks import GrafomemGovernanceHook


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.memories.write.return_value = MagicMock(ref="ref-456")
    client.memories.retrieve.return_value = [
        MagicMock(content="context 1", score=0.9, meta={"role": "user", "session": "default", "sender": "agent-1"}),
        MagicMock(content="context 2", score=0.8, meta={"role": "assistant", "session": "default", "sender": "agent-2"}),
    ]
    client.stores.flush.return_value = None
    client.governance.evaluate.return_value = {"verdict": "allow"}
    client.decisions.log.return_value = {"decision_id": "d-456"}
    return client


class TestGrafomemAutoGenMemory:
    def test_add_message(self, mock_client):
        mem = GrafomemAutoGenMemory(mock_client, "store-1")
        mem.add_message("Hello world", role="user", sender="human")
        mock_client.memories.write.assert_called_once()
        meta = mock_client.memories.write.call_args[1]["meta"]
        assert meta["role"] == "user"
        assert meta["sender"] == "human"
        assert meta["type"] == "autogen_message"

    def test_get_context(self, mock_client):
        mem = GrafomemAutoGenMemory(mock_client, "store-1")
        ctx = mem.get_context("compliance requirements")
        assert len(ctx) == 2
        assert ctx[0]["content"] == "context 1"
        mock_client.memories.retrieve.assert_called_once()

    def test_get_messages_all(self, mock_client):
        mem = GrafomemAutoGenMemory(mock_client, "store-1")
        msgs = mem.get_messages()
        assert len(msgs) == 2

    def test_get_messages_filtered_by_sender(self, mock_client):
        mem = GrafomemAutoGenMemory(mock_client, "store-1")
        msgs = mem.get_messages(sender="agent-1")
        assert len(msgs) == 1
        assert msgs[0]["sender"] == "agent-1"

    def test_clear_flushes_store(self, mock_client):
        mem = GrafomemAutoGenMemory(mock_client, "store-1")
        mem.clear()
        mock_client.stores.flush.assert_called_once_with("store-1")

    def test_custom_session_id(self, mock_client):
        mem = GrafomemAutoGenMemory(mock_client, "store-1", session_id="sess-42")
        mem.add_message("test", role="user")
        meta = mock_client.memories.write.call_args[1]["meta"]
        assert meta["session"] == "sess-42"

    def test_custom_source(self, mock_client):
        mem = GrafomemAutoGenMemory(mock_client, "store-1", source="my-app")
        mem.add_message("test")
        assert mock_client.memories.write.call_args[1]["source"] == "my-app"


class TestGrafomemGovernanceHook:
    def test_pre_send_allow(self, mock_client):
        hook = GrafomemGovernanceHook(mock_client)
        result = hook.pre_send("user", "Hello", "assistant")
        assert result == "Hello"  # Message passes through
        mock_client.governance.evaluate.assert_called_once()

    def test_pre_send_deny_blocks(self, mock_client):
        mock_client.governance.evaluate.return_value = {"verdict": "deny", "reason": "blocked"}
        hook = GrafomemGovernanceHook(mock_client)
        result = hook.pre_send("user", "Dangerous message", "assistant")
        assert result is None  # Message blocked

    def test_pre_send_deny_no_block(self, mock_client):
        mock_client.governance.evaluate.return_value = {"verdict": "deny", "reason": "x"}
        hook = GrafomemGovernanceHook(mock_client, block_on_deny=False)
        result = hook.pre_send("user", "Message", "assistant")
        assert result == "Message"  # Message passes through despite deny

    def test_post_receive_logs_decision(self, mock_client):
        hook = GrafomemGovernanceHook(mock_client)
        hook.post_receive("assistant", "Response text")
        mock_client.decisions.log.assert_called_once()

    def test_governance_error_swallowed(self, mock_client):
        mock_client.governance.evaluate.side_effect = ConnectionError("timeout")
        hook = GrafomemGovernanceHook(mock_client)
        result = hook.pre_send("user", "Hello", "assistant")
        assert result == "Hello"  # Error swallowed, message allowed
