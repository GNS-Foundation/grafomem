import re

with open("src/aml/backends/postgres_gmp.py", "r") as f:
    text = f.read()

# Add imports
text = text.replace("import psycopg\n", "import psycopg\n            from psycopg_pool import ConnectionPool\n            from contextlib import contextmanager\n")

# Replace __init__
init_old = """        self._conn = psycopg.connect(db_url, autocommit=True)
        with self._conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        register_vector(self._conn)
        self._ensure_schema()"""

init_new = """        self._pool = ConnectionPool(db_url, min_size=1, max_size=20)
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            register_vector(conn)
        self._ensure_schema()"""
text = text.replace(init_old, init_new)

tenant_conn = """
    @contextmanager
    def _tenant_conn(self, tenant_id: str):
        with self._pool.connection() as conn:
            from pgvector.psycopg import register_vector
            register_vector(conn)
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL app.current_tenant = %s", (tenant_id,))
                    yield conn, cur
                    
    def _ensure_schema(self) -> None:"""
text = text.replace("    def _ensure_schema(self) -> None:", tenant_conn)

rls = """            cur.execute(_TENANT_FILTER_INDEX.strip())

            # Enable RLS
            cur.execute("ALTER TABLE memories ENABLE ROW LEVEL SECURITY")
            cur.execute(
                \"\"\"
                DO $$ BEGIN
                    CREATE POLICY tenant_isolation_memories ON memories
                        USING (tenant_id = current_setting('app.current_tenant', true) OR current_setting('app.current_tenant', true) = 'admin');
                EXCEPTION
                    WHEN duplicate_object THEN null;
                END $$;
                \"\"\"
            )
            cur.execute("ALTER TABLE memory_embeddings ENABLE ROW LEVEL SECURITY")
            cur.execute(
                \"\"\"
                DO $$ BEGIN
                    CREATE POLICY tenant_isolation_embeddings ON memory_embeddings
                        USING (tenant_id = current_setting('app.current_tenant', true) OR current_setting('app.current_tenant', true) = 'admin');
                EXCEPTION
                    WHEN duplicate_object THEN null;
                END $$;
                \"\"\"
            )
"""
text = text.replace("            cur.execute(_TENANT_FILTER_INDEX.strip())", rls)


# Replace schema ensure and storage_bytes
text = text.replace("        with self._conn.cursor() as cur:\n            # Memories table", "        with self._pool.connection() as conn:\n            with conn.cursor() as cur:\n                # Memories table")
text = text.replace("        try:\n            with self._conn.cursor() as cur:\n                cur.execute(", "        try:\n            with self._pool.connection() as conn:\n                with conn.cursor() as cur:\n                    cur.execute(")

# Replace all occurrences of `with self._conn.cursor() as cur:` that are for tenant operations
# write()
text = re.sub(r"with self\._conn\.cursor\(\) as cur:\n\s+cur\.execute\(\n\s+\"\"\"INSERT INTO memories", 
              r"with self._tenant_conn(options.tenant_id) as (conn, cur):\n            cur.execute(\n                \"\"\"INSERT INTO memories", text)

# write_many()
text = re.sub(r"with self\._conn\.transaction\(\):\n\s+with self\._conn\.cursor\(\) as cur:\n\s+for \(content, options\), row in zip\(items, embs\):",
              r"for (content, options), row in zip(items, embs):\n                with self._tenant_conn(options.tenant_id) as (conn, cur):", text)

# supersede()
text = re.sub(r"with self\._conn\.cursor\(\) as cur:\n\s+# Close predecessor",
              r"with self._tenant_conn(options.tenant_id) as (conn, cur):\n            # Close predecessor", text)

# retrieve() check embeddings
text = re.sub(r"with self\._conn\.cursor\(\) as cur:\n\s+cur\.execute\(\"SELECT COUNT",
              r"with self._tenant_conn(options.tenant_id) as (conn, cur):\n            cur.execute(\"SELECT COUNT", text)

# retrieve() execute query
text = re.sub(r"with self\._conn\.cursor\(\) as cur:\n\s+# pgvector cosine distance",
              r"with self._tenant_conn(options.tenant_id) as (conn, cur):\n            # pgvector cosine distance", text)

# retrieve() fetch items
text = re.sub(r"for \(ref,\) in ranked:\n\s+with self\._conn\.cursor\(\) as cur:\n\s+cur\.execute\(",
              r"for (ref,) in ranked:\n            with self._tenant_conn(options.tenant_id) as (conn, cur):\n                cur.execute(", text)

# audit()
text = re.sub(r"with self\._conn\.cursor\(\) as cur:\n\s+cur\.execute\(\n\s+\"\"\"SELECT ref",
              r"with self._tenant_conn('admin') as (conn, cur):\n            cur.execute(\n                \"\"\"SELECT ref", text)

# close()
text = text.replace("self._conn.close()", "self._pool.close()")

# smoke test cleanup
text = text.replace("    with b._conn.cursor() as cur:\n        cur.execute(\"DELETE FROM memories\")\n\n    t0", "    with b._pool.connection() as conn:\n        with conn.cursor() as cur:\n            cur.execute(\"DELETE FROM memories\")\n\n    t0")
text = text.replace("    with b._conn.cursor() as cur:\n        cur.execute(\"DELETE FROM memories\")\n    b.close()", "    with b._pool.connection() as conn:\n        with conn.cursor() as cur:\n            cur.execute(\"DELETE FROM memories\")\n    b.close()")

with open("src/aml/backends/postgres_gmp.py", "w") as f:
    f.write(text)

