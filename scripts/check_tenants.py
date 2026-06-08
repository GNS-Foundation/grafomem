import os, psycopg

def main():
    db_url = os.environ.get('GRAFOMEM_DB_URL')
    if not db_url: return
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT tenant_id, COUNT(*) FROM orchestrator_steps GROUP BY tenant_id;")
            for row in cur.fetchall():
                print(row)

if __name__ == "__main__":
    main()
