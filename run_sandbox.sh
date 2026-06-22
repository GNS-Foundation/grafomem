#!/bin/bash
set -e
export GRAFOMEM_DB_URL=postgresql://grafomem:grafomem_dev@localhost:5432/grafomem
export PYTHONPATH=src
export PROVIDER_ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
export GRAFOMEM_MASTER_KEY=$(python3 -c "import os; print(os.urandom(32).hex())")
export PORTAL_SECRET_KEY=$(python3 -c "import os; print(os.urandom(32).hex())")

# Clear tables before test
psql postgresql://grafomem:grafomem_dev@localhost:5432/grafomem -c "TRUNCATE TABLE tenant_members CASCADE; TRUNCATE TABLE tenants CASCADE;" || true

python3 src/aml/cli.py serve -b postgres --db $GRAFOMEM_DB_URL --port 8080 > uvicorn.log 2>&1 &
PID=$!
sleep 5
python3 tests/sandbox_e2e_v2.py
kill $PID
