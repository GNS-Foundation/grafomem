import psycopg
DB_URL = "postgresql://postgres:sfhGsiZUCdgdYztEnhSKQlQSSPWwWHGT@postgres.railway.internal:5432/railway"
with psycopg.connect(DB_URL) as conn:
    row = conn.execute("SELECT api_key FROM llm_providers WHERE provider = 'openai' LIMIT 1").fetchone()
    print("Found API KEY:", row is not None)
    if row:
        print("Key begins with:", row[0][:8])
