import uvicorn
from aml.server.app import create_app
import os

DB_URL = "sqlite:///:memory:"
MASTER_KEY = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
os.environ["PROVIDER_ENCRYPTION_KEY"] = "VGVzdEVuY3J5cHRpb25LZXkxMjM0NTY3ODkwMTIzNDU="
os.environ["GRAFOMEM_SIGNING_KEY"] = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
os.environ["GRAFOMEM_MASTER_KEY"] = MASTER_KEY

def _factory():
    from aml.backends.sqlite_gmp import SQLiteGMPBackend
    return SQLiteGMPBackend(db_path=":memory:")

app = create_app(
    backend_factory=_factory,
    auth_mode="cloud",
    db_url=DB_URL,
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8643)
