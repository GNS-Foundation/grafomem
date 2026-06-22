import os
import psycopg

BASE_URL = "http://localhost:8080"

db_url = "postgresql://grafomem:dev@localhost:5432/grafomem"

with psycopg.connect(db_url) as conn:
    for row in conn.execute("SELECT name, expires_at, ip_allowlist FROM tenant_api_keys ORDER BY created_at DESC LIMIT 3"):
        print(row)
