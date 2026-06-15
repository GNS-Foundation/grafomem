import psycopg
import os

url = os.environ.get("GRAFOMEM_DB_URL", "postgresql://grafomem:grafomem_dev@localhost:5432/grafomem")
with psycopg.connect(url) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT tenant_id, provider, api_key FROM llm_providers LIMIT 1")
        row = cur.fetchone()
        if row:
            print(f"RAW SQL DUMP: tenant_id={row[0]}, provider={row[1]}, api_key={row[2]}")
        else:
            print("No rows found in llm_providers")
