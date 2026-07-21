import uvicorn
from aml.server.app import create_app
import os

db_url = os.environ.get("GRAFOMEM_DB_URL")
app = create_app(db_url=db_url, spec_only=False)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)
