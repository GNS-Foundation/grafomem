#!/usr/bin/env python3
import psycopg2
import os

DB_URL = os.environ.get("DATABASE_URL", "dbname=grafomem")

def main():
    print(f"Connecting to {DB_URL}...")
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    
    with conn.cursor() as cur:
        # Drop all synthetic steps
        cur.execute("DELETE FROM orchestrator_steps WHERE is_synthetic = true")
        deleted_steps = cur.rowcount
        print(f"Deleted {deleted_steps} synthetic steps from orchestrator_steps.")
        
        # Clear manifold cache to force retraining on next request
        cur.execute("DELETE FROM manifold_cache")
        deleted_cache = cur.rowcount
        print(f"Cleared {deleted_cache} tenants from manifold_cache.")
        
    conn.close()
    print("Done! The Manifold will be retrained on real steps on the next /v1/manifold/export request.")

if __name__ == "__main__":
    main()
