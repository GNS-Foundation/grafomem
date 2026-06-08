import os, psycopg

def main():
    db_url = os.environ.get('GRAFOMEM_DB_URL')
    if not db_url:
        print("No GRAFOMEM_DB_URL found!")
        return
    with psycopg.connect(db_url) as conn:
        conn.execute("ALTER TABLE orchestrator_steps ADD COLUMN IF NOT EXISTS is_synthetic BOOLEAN NOT NULL DEFAULT false;")
        conn.execute("UPDATE orchestrator_steps SET is_synthetic = true;")
        
    print("Migration and backfill completed successfully.")

if __name__ == "__main__":
    main()
