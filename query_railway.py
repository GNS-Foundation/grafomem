import psycopg

db_url = "postgresql://postgres:sfhGsiZUCdgdYztEnhSKQlQSSPWwWHGT@kodama.proxy.rlwy.net:57503/railway"

with psycopg.connect(db_url) as conn:
    with conn.cursor() as cur:
        # Check llm_providers
        print("--- QUERY: llm_providers ---")
        cur.execute("SELECT count(*) FROM llm_providers WHERE api_key LIKE 'gAAAAA%'")
        print(f"llm_providers matching 'gAAAAA%': {cur.fetchone()[0]}")
        cur.execute("SELECT count(*) FROM llm_providers WHERE api_key LIKE 'sk-%' OR (api_key NOT LIKE 'gAAAAA%' AND api_key != 'not-needed')")
        print(f"llm_providers plaintext matching 'sk-%' or equivalent: {cur.fetchone()[0]}")
        
        # Check tenants
        print("--- QUERY: tenants ---")
        cur.execute("SELECT count(*) FROM tenants WHERE api_key LIKE 'gAAAAA%'")
        print(f"tenants matching 'gAAAAA%': {cur.fetchone()[0]}")
        cur.execute("SELECT count(*) FROM tenants WHERE api_key LIKE 'sk-%' OR (api_key NOT LIKE 'gAAAAA%' AND api_key != 'not-needed')")
        print(f"tenants plaintext matching 'sk-%' or equivalent: {cur.fetchone()[0]}")
