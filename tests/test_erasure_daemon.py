import os
import pytest
from unittest.mock import patch, MagicMock
from prometheus_client import REGISTRY

from aml.cloud.erasure_daemon import run_sweep_job

@pytest.fixture
def get_metric():
    # Helper to get current metric values
    def _get_metric(name):
        val = REGISTRY.get_sample_value(name)
        return val if val is not None else 0.0
    return _get_metric

def test_run_sweep_job_success(get_metric):
    initial_sweeps = get_metric("grafomem_erasure_sweeps_total")
    initial_swept = get_metric("grafomem_erasure_embeddings_swept_total")
    
    with patch("aml.cloud.erasure_daemon.ErasureSweeper") as MockSweeper:
        instance = MockSweeper.return_value
        instance.sweep.return_value = 5  # swept 5 embeddings
        
        run_sweep_job("mock_db_url")
        
        # Check metrics
        assert get_metric("grafomem_erasure_sweeps_total") == initial_sweeps + 1
        assert get_metric("grafomem_erasure_embeddings_swept_total") == initial_swept + 5

def test_run_sweep_job_failure(get_metric):
    initial_errors = get_metric("grafomem_erasure_sweep_errors_total")
    
    with patch("aml.cloud.erasure_daemon.ErasureSweeper") as MockSweeper:
        instance = MockSweeper.return_value
        instance.sweep.side_effect = Exception("DB Connection Failed")
        
        run_sweep_job("mock_db_url")
        
        # Error metric should increment
        assert get_metric("grafomem_erasure_sweep_errors_total") == initial_errors + 1
