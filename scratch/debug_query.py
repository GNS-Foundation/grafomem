import psycopg
db_url = "postgresql://grafomem:dev@localhost:5432/grafomem"
with psycopg.connect(db_url) as conn:
    row = conn.execute("SELECT * FROM orchestrator_steps ORDER BY created_at DESC LIMIT 1").fetchone()
    cols = [desc[0] for desc in conn.execute("SELECT * FROM orchestrator_steps LIMIT 0").description]
    for k, v in zip(cols, row):
        print(f"{k}: {repr(v)[:200]}")
