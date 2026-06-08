#!/bin/bash
set -e

# Generate deterministic keys for the local test run
SEED=$(python3 -c "import os; print(os.urandom(32).hex())")
FERNET=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

export GRAFOMEM_SIGNING_KEY=$SEED
export PROVIDER_ENCRYPTION_KEY=$FERNET
export GRAFOMEM_DB_URL=postgresql://grafomem:grafomem_dev@localhost:5432/grafomem
export PYTHONPATH=src

echo "=========================================================="
echo " GRAFOMEM CLOUD : PHASE 1 LIVE FLIGHT"
echo "=========================================================="

echo "[*] Starting local live REST server..."
python3 src/aml/cli.py serve -b postgres --db "$GRAFOMEM_DB_URL" --port 8642 > server.log 2>&1 &
SERVER_PID=$!

# Wait for server to boot
sleep 4

echo "[*] Provisioning test tenant via DB..."
python3 -c "
import os
import psycopg
conn = psycopg.connect(os.environ['GRAFOMEM_DB_URL'])
conn.execute(\"INSERT INTO tenants (id, name, api_key) VALUES ('test_flight_tenant', 'Phase 1 Flight', 'dummy_flight_token') ON CONFLICT DO NOTHING\")
conn.commit()
conn.close()
"

echo "[*] Running LIVE conformance flight against REST API..."
# We use the live REST endpoint, sending real HTTP traffic!
# We pass the api key 'dummy_flight_token' which resolves to 'test_flight_tenant'.
python3 src/aml/cli.py conformance --url http://127.0.0.1:8642 --token "dummy_flight_token" --seeds 1 > flight_results.txt 2>&1

echo "[*] Flight completed. Shutting down REST server..."
kill $SERVER_PID

cat flight_results.txt
