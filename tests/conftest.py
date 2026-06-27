import os
from cryptography.fernet import Fernet
import pytest

# Inject an ephemeral Fernet key for all tests to bypass strict db_url key requirements
import secrets
os.environ["PROVIDER_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["GRAFOMEM_MASTER_KEY"] = secrets.token_hex(32)
os.environ["GRAFOMEM_LEDGER_URL"] = "postgresql://grafomem:dev@localhost:5432/grafomem_ledger"
os.environ["UNSAFE_LOCAL_DEV"] = "true"

