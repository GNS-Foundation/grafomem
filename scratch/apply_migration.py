import os
import psycopg

db_url = os.environ.get("GRAFOMEM_DB_URL") or os.environ.get("DATABASE_URL")
if not db_url:
    print("NO DATABASE URL!")
    import sys; sys.exit(1)

with open("src/aml/backends/migrations/004_token_boundaries.sql", "r") as f:
    sql = f.read()

try:
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()
    print("SUCCESS: 004_token_boundaries.sql applied cleanly.")
except Exception as e:
    print(f"FAILED: {e}")
