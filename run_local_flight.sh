#!/bin/bash
set -e

# Setup keys
SEED=$(python3 -c "import os; print(os.urandom(32).hex())")
FERNET=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

export GRAFOMEM_SIGNING_KEY=$SEED
export PROVIDER_ENCRYPTION_KEY=$FERNET
export GRAFOMEM_DB_URL=postgresql://grafomem:grafomem_dev@localhost:5432/grafomem
export PYTHONPATH=src

# Start uvicorn in background
echo "Starting uvicorn on port 8000..."
uvicorn aml.server.app:app --port 8000 > uvicorn.log 2>&1 &
UVICORN_PID=$!

# Wait for it to boot
sleep 3

# Run the flight script
python3 scripts/phase1_live_flight.py

# Kill uvicorn
kill $UVICORN_PID
