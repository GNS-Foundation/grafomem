import re
with open("src/aml/backends/postgres_gmp.py", "r") as f:
    code = f.read()

# Add contextmanager import
if "from contextlib import contextmanager" not in code:
    code = code.replace("from typing import Any", "from contextlib import contextmanager\nfrom typing import Any")

# Add _tenant_cursor method
tenant_cursor = """
    @contextmanager
    def _tenant_cursor(self, tenant_id: str):
        with self._conn.transaction():
            with self._conn.cursor() as cur:
                cur.execute("SET LOCAL app.current_tenant = %s", (tenant_id,))
                yield cur
"""
if "def _tenant_cursor" not in code:
    code = code.replace("def _ensure_schema(self) -> None:", tenant_cursor.strip() + "\n\n    def _ensure_schema(self) -> None:")

# Replace with self._conn.cursor() as cur: -> with self._tenant_cursor(...) as cur:
code = re.sub(
    r"with self\._conn\.cursor\(\) as cur:\n\s+cur\.execute\(\n\s+\"\"\"INSERT INTO memories",
    r"with self._tenant_cursor(options.tenant_id) as cur:\n            cur.execute(\n                \"\"\"INSERT INTO memories",
    code
)

code = re.sub(
    r"with self\._conn\.transaction\(\):\n\s+with self\._conn\.cursor\(\) as cur:\n\s+for \(content, options\), row in zip\(items, embs\):",
    r"for (content, options), row in zip(items, embs):\n                with self._tenant_cursor(options.tenant_id) as cur:",
    code
)

# And fix RLS in _ensure_schema
rls_sql = """
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
if "ENABLE ROW LEVEL SECURITY" not in code:
    code = code.replace("cur.execute(_TENANT_FILTER_INDEX.strip())", "cur.execute(_TENANT_FILTER_INDEX.strip())\n" + rls_sql)

with open("src/aml/backends/postgres_gmp.py", "w") as f:
    f.write(code)
