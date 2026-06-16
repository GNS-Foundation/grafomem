import pytest
from cryptography.fernet import Fernet
from aml.cloud.identity import EnvIdentity
from aml.backends.sqlite_gmp import SQLiteGMPBackend
from aml.backends.postgres_gmp import PostgresGMPBackend
from aml.backends.interface import WriteOptions, RetrieveOptions

def test_encryption_at_rest_sqlite(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("PROVIDER_ENCRYPTION_KEY", key)
    
    identity = EnvIdentity()
    store = SQLiteGMPBackend(":memory:", encryption=identity)
    
    ref = store.write("secret data", WriteOptions())
    results = store.retrieve("secret data", RetrieveOptions())
    assert len(results) > 0
    assert results[0].content == "secret data"

