import psycopg
import os
import uuid
from datetime import datetime, timezone, timedelta

db_url = os.environ.get("DATABASE_PUBLIC_URL", "postgresql://postgres:sfhGsiZUCdgdYztEnhSKQlQSSPWwWHGT@kodama.proxy.rlwy.net:57503/railway")

def verify_erasure():
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            # 1. Generate a test embedding
            import random
            test_ref = random.randint(1000000000, 2000000000)
            
            # We set the pending time to 120 minutes ago to ensure it is immediately eligible for sweeping.
            past_time = datetime.now(timezone.utc) - timedelta(minutes=120)
            
            # 2. Insert test embedding
            # schema: memory_embeddings (ref, embedding, tenant_id, valid_from, valid_until, erasure_pending)
            dummy_vec = [0.0] * 384
            cur.execute("""
                INSERT INTO memory_embeddings 
                (ref, embedding, tenant_id, valid_from, valid_until, erasure_pending)
                VALUES (%s, %s::vector, %s, %s, %s, %s)
            """, (
                test_ref, dummy_vec, 'test_tenant', 
                past_time, past_time, past_time
            ))
            conn.commit()
            
            print(f"✅ INSERTED test embedding with ref: {test_ref}")
            print(f"   Erasure Pending Time: {past_time.isoformat()}")
            
            # 3. Report current pending counts and ages
            cur.execute("""
                SELECT ref, erasure_pending, EXTRACT(EPOCH FROM (now() - erasure_pending))/60 as age_minutes 
                FROM memory_embeddings 
                WHERE erasure_pending IS NOT NULL
                ORDER BY age_minutes DESC
            """)
            rows = cur.fetchall()
            
            print("\n📊 CURRENT PENDING ERASURES:")
            if not rows:
                print("   None.")
            for row in rows:
                ref, pending_time, age = row
                marker = "👈 TEST REF" if ref == test_ref else ""
                print(f"   - {ref} | Pending Since: {pending_time} ({age:.1f} mins old) {marker}")
                
            print("\nNext Steps:")
            print("1. Deploy the erasure_daemon to Railway.")
            print("2. Run this script again (or write a loop) to check if the TEST REF disappears!")

if __name__ == "__main__":
    verify_erasure()
