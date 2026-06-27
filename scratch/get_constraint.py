import psycopg
conn = psycopg.connect("postgresql://postgres:postgres@localhost:5432/grafomem")
res = conn.execute("""
    SELECT conname
    FROM pg_constraint
    WHERE conrelid = 'memory_embeddings'::regclass
      AND contype = 'f';
""").fetchall()
print(res)
