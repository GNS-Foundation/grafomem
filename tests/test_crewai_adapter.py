"""GRAFOMEM CrewAI Adapter Tests — Sprint 18.

DB-free tests verifying CrewAI storage and governance callbacks
against a mock GrafomemClient. Does NOT require crewai package.
"""
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch
import pytest

# ── Mock crewai before importing adapter ──
crewai_mock = ModuleType("crewai")
crewai_memory_mock = ModuleType("crewai.memory")
crewai_storage_mock = ModuleType("crewai.memory.storage")
crewai_interface_mock = ModuleType("crewai.memory.storage.interface")

# Create a mock Storage base class
class MockStorage:
    pass

crewai_interface_mock.Storage = MockStorage
crewai_storage_mock.interface = crewai_interface_mock
crewai_memory_mock.storage = crewai_storage_mock
crewai_mock.memory = crewai_memory_mock

sys.modules["crewai"] = crewai_mock
sys.modules["crewai.memory"] = crewai_memory_mock
sys.modules["crewai.memory.storage"] = crewai_storage_mock
sys.modules["crewai.memory.storage.interface"] = crewai_interface_mock

# Now we can import
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdk" / "src"))

from grafomem.crewai.storage import GrafomemCrewStorage
from grafomem.crewai.callbacks import GrafomemGovernanceCallback, GovernanceDeniedError


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.memories.write.return_value = MagicMock(ref="ref-123")
    client.memories.retrieve.return_value = [
        MagicMock(content="memory 1", score=0.9, meta={"type": "crew_memory"}),
        MagicMock(content="memory 2", score=0.7, meta={"type": "crew_memory"}),
    ]
    client.stores.flush.return_value = None
    client.governance.evaluate.return_value = {"verdict": "allow"}
    client.decisions.log.return_value = {"decision_id": "d-123"}
    return client


class TestGrafomemCrewStorage:
    def test_save_string(self, mock_client):
        storage = GrafomemCrewStorage(mock_client, "store-1")
        storage.save("test memory")
        mock_client.memories.write.assert_called_once()
        args = mock_client.memories.write.call_args
        assert args[0][0] == "store-1"
        assert args[1]["content"] == "test memory"
        assert args[1]["source"] == "crewai"

    def test_save_dict_serializes_to_json(self, mock_client):
        storage = GrafomemCrewStorage(mock_client, "store-1")
        storage.save({"key": "value"})
        args = mock_client.memories.write.call_args
        assert '"key"' in args[1]["content"]

    def test_save_with_metadata(self, mock_client):
        storage = GrafomemCrewStorage(mock_client, "store-1")
        storage.save("test", metadata={"agent": "researcher"})
        meta = mock_client.memories.write.call_args[1]["meta"]
        assert meta["agent"] == "researcher"
        assert meta["type"] == "crew_memory"

    def test_search_returns_results(self, mock_client):
        storage = GrafomemCrewStorage(mock_client, "store-1")
        results = storage.search("compliance", limit=2)
        assert len(results) == 2
        assert results[0]["context"] == "memory 1"
        mock_client.memories.retrieve.assert_called_once()

    def test_reset_flushes_store(self, mock_client):
        storage = GrafomemCrewStorage(mock_client, "store-1")
        storage.reset()
        mock_client.stores.flush.assert_called_once_with("store-1")

    def test_custom_source(self, mock_client):
        storage = GrafomemCrewStorage(mock_client, "store-1", source="custom")
        storage.save("test")
        assert mock_client.memories.write.call_args[1]["source"] == "custom"


class TestGrafomemGovernanceCallback:
    def test_on_task_start_allow(self, mock_client):
        cb = GrafomemGovernanceCallback(mock_client)
        cb.on_task_start("Research compliance")  # Should not raise
        mock_client.governance.evaluate.assert_called_once()

    def test_on_task_start_deny_raises(self, mock_client):
        mock_client.governance.evaluate.return_value = {
            "verdict": "deny", "reason": "rate limited", "policy_id": "p-1"
        }
        cb = GrafomemGovernanceCallback(mock_client)
        with pytest.raises(GovernanceDeniedError) as exc_info:
            cb.on_task_start("Dangerous task")
        assert "rate limited" in str(exc_info.value)

    def test_on_task_start_deny_no_raise(self, mock_client):
        mock_client.governance.evaluate.return_value = {"verdict": "deny", "reason": "x"}
        cb = GrafomemGovernanceCallback(mock_client, deny_raises=False)
        cb.on_task_start("Task")  # Should not raise

    def test_on_task_end_logs_decision(self, mock_client):
        cb = GrafomemGovernanceCallback(mock_client)
        cb.on_task_end("Task completed successfully")
        mock_client.decisions.log.assert_called_once()

    def test_governance_error_is_swallowed(self, mock_client):
        mock_client.governance.evaluate.side_effect = ConnectionError("timeout")
        cb = GrafomemGovernanceCallback(mock_client)
        cb.on_task_start("Task")  # Should not raise, error is swallowed
