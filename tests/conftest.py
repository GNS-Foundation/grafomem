import os
from cryptography.fernet import Fernet
import pytest

# Inject an ephemeral Fernet key for all tests to bypass strict db_url key requirements
os.environ["PROVIDER_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
