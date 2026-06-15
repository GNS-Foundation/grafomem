import psycopg
import os
from cryptography.fernet import Fernet
from aml.cloud.identity import EnvIdentity

# EnvIdentity uses the env variables, so set it
os.environ["PROVIDER_ENCRYPTION_KEY"] = "1Bv39muZABKzp0r3hRdXnCYTYlYfbPhREGINKDbKJ2M="

encryption = EnvIdentity()

db_url = "postgresql://postgres:sfhGsiZUCdgdYztEnhSKQlQSSPWwWHGT@kodama.proxy.rlwy.net:57503/railway"

try:
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT config_id, api_key FROM llm_providers WHERE api_key IS NOT NULL AND api_key NOT LIKE 'gAAAAA%';")
            rows = cur.fetchall()
            print(f"Found {len(rows)} unencrypted keys in PROD.")
            
            for config_id, api_key in rows:
                enc_key = encryption.encrypt(api_key)
                cur.execute(
                    "UPDATE llm_providers SET api_key = %s WHERE config_id = %s;",
                    (enc_key, config_id)
                )
            conn.commit()
            print("Successfully migrated all keys.")
            
            # Verify
            cur.execute("SELECT api_key FROM llm_providers WHERE api_key IS NOT NULL LIMIT 5;")
            ver_rows = cur.fetchall()
            print("\nRAW SQL DUMP FROM PROD (POST-MIGRATION):")
            for r in ver_rows:
                print(r[0])
except Exception as e:
    print(f"Error: {e}")
