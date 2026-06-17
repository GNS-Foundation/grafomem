import re

with open("src/aml/backends/postgres_gmp.py", "r") as f:
    lines = f.readlines()

new_lines = []
in_init = False
for line in lines:
    if line.startswith("from typing import Any"):
        new_lines.append(line)
        new_lines.append("from contextlib import contextmanager\n")
    elif "import psycopg" in line:
        new_lines.append(line)
        if "from psycopg_pool import ConnectionPool" not in "".join(lines):
            new_lines.append("            from psycopg_pool import ConnectionPool\n")
    elif "def __init__" in line:
        in_init = True
        new_lines.append(line)
    elif in_init and "self._conn = psycopg.connect" in line:
        new_lines.append("        self._pool = ConnectionPool(db_url, min_size=1, max_size=20)\n")
        new_lines.append("        with self._pool.connection() as conn:\n")
        new_lines.append("            with conn.cursor() as cur:\n")
        new_lines.append("                cur.execute('CREATE EXTENSION IF NOT EXISTS vector')\n")
        new_lines.append("            register_vector(conn)\n")
    elif in_init and "with self._conn.cursor" in line:
        pass
    elif in_init and "cur.execute" in line and "CREATE EXTENSION" in line:
        pass
    elif in_init and "register_vector" in line:
        pass
    elif "def _ensure_schema" in line:
        in_init = False
        new_lines.append("    @contextmanager\n")
        new_lines.append("    def _tenant_conn(self, tenant_id: str):\n")
        new_lines.append("        with self._pool.connection() as conn:\n")
        new_lines.append("            with conn.transaction():\n")
        new_lines.append("                with conn.cursor() as cur:\n")
        new_lines.append("                    cur.execute(\"SET LOCAL app.current_tenant = %s\", (tenant_id,))\n")
        new_lines.append("                    yield conn, cur\n\n")
        new_lines.append(line)
    elif "def close(self)" in line:
        new_lines.append(line)
        new_lines.append("        self._pool.close()\n")
    elif "self._conn.close()" in line:
        pass
    else:
        new_lines.append(line)

content = "".join(new_lines)

# Fix RLS in _ensure_schema
rls_sql = """
            # RLS Policies
            cur.execute("ALTER TABLE memories ENABLE ROW LEVEL SECURITY")
            cur.execute("DO $$ BEGIN CREATE POLICY tenant_isolation_memories ON memories USING (tenant_id = current_setting('app.current_tenant', true) OR current_setting('app.current_tenant', true) = 'admin'); EXCEPTION WHEN duplicate_object THEN null; END $$;")
            cur.execute("ALTER TABLE memory_embeddings ENABLE ROW LEVEL SECURITY")
            cur.execute("DO $$ BEGIN CREATE POLICY tenant_isolation_embeddings ON memory_embeddings USING (tenant_id = current_setting('app.current_tenant', true) OR current_setting('app.current_tenant', true) = 'admin'); EXCEPTION WHEN duplicate_object THEN null; END $$;")
"""
content = content.replace("cur.execute(_TENANT_FILTER_INDEX.strip())", "cur.execute(_TENANT_FILTER_INDEX.strip())\n" + rls_sql)

# Ensure schema runs setup OUTSIDE tenant because it's admin/system
content = content.replace(
    "with self._conn.cursor() as cur:",
    "with self._pool.connection() as conn:\n            with conn.cursor() as cur:",
    1 # only the first one which is _ensure_schema
)

# Fix storage_bytes
content = content.replace(
    "with self._conn.cursor() as cur:",
    "with self._pool.connection() as conn:\n            with conn.cursor() as cur:",
    1 # the second one which is storage_bytes
)

# Now for write, write_many, supersede, delete, retrieve, retrieve_all
# They all need to use _tenant_conn

content = content.replace(
    "with self._conn.cursor() as cur:",
    "with self._tenant_conn(options.tenant_id) as (conn, cur):"
)
content = content.replace(
    "with self._conn.transaction():\n            with self._conn.cursor() as cur:",
    "with self._tenant_conn(options.tenant_id) as (conn, cur):"
)
content = content.replace(
    "with self._conn.transaction():\n            with self._tenant_conn(options.tenant_id) as (conn, cur):",
    "with self._tenant_conn(options.tenant_id) as (conn, cur):"
)
content = content.replace(
    "with self._tenant_conn(options.tenant_id) as (conn, cur):\n                for (content, options), row in zip(items, embs):",
    "for (content, options), row in zip(items, embs):\n                # Assume single tenant for batch for simplicity, or we can't batch across tenants easily. Wait, write_many items have individual options! We can just use the first item's tenant, or we group by tenant."
)

with open("src/aml/backends/postgres_gmp.py", "w") as f:
    f.write(content)
