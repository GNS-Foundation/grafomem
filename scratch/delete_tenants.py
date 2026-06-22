import sys
import os
import psycopg
sys.path.insert(0, os.path.abspath('src'))

db_url = os.environ.get("GRAFOMEM_DB_URL")
if not db_url:
    print("No DB URL")
    sys.exit(1)

with psycopg.connect(db_url) as conn:
    conn.execute("DELETE FROM tenants")
    conn.commit()
    print("Cleared all tenants from DB to reset DEKs.")
