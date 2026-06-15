import psycopg
import json

db_url = "postgresql://postgres:sfhGsiZUCdgdYztEnhSKQlQSSPWwWHGT@kodama.proxy.rlwy.net:57503/railway"

try:
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            # Look at a sample of keys to see if they are plaintext (like sk-...) or Fernet (like gAAAAA...)
            cur.execute("SELECT api_key FROM llm_providers WHERE api_key IS NOT NULL LIMIT 5;")
            rows = cur.fetchall()
            print("RAW SQL DUMP FROM PROD:")
            for r in rows:
                print(r[0])
except Exception as e:
    print(f"Error: {e}")
