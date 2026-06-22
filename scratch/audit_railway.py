import psycopg
import os

db_url = os.environ.get("GRAFOMEM_DB_URL")

with psycopg.connect(db_url) as conn:
    cols = conn.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'tenant_api_keys'
        ORDER BY ordinal_position;
    """).fetchall()
    
    print("--- tenant_api_keys SCHEMA ---")
    for col in cols:
        print(col)
