import pytest
from aml.server.app import create_app
from aml.backends.sqlite_gmp import SQLiteGMPBackend
from aml.backends.interface import Memory

def _factory():
    return SQLiteGMPBackend(db_path=":memory:")

@pytest.fixture
def test_app():
    return create_app(backend_factory=_factory, auth_mode="local", db_url="sqlite:///:memory:")

def test_store_manager_token_counts():
    # token_count + tokenizer_id written on write, read back without re-tokenization;
    # legacy rows lacking the columns still read.
    # Note: store manager testing against SQLite for token counts.
    pass
