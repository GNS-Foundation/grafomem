import os
import psycopg2

db_url = os.environ.get("DATABASE_URL")
if not db_url:
    print("NO DB URL")
    exit(1)

conn = psycopg2.connect(db_url)
with conn.cursor() as cur:
    # Check if there are any embeddings that are orphaned
    cur.execute("SELECT count(*) FROM memory_embeddings WHERE memory_id NOT IN (SELECT id FROM memories) OR status = 'erasure_pending'")
    row = cur.fetchone()
    print("Orphaned/Pending embeddings count:", row[0])
