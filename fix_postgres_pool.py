import re
with open("src/aml/backends/postgres_gmp.py", "r") as f:
    code = f.read()

# Add ConnectionPool import
if "from psycopg_pool import ConnectionPool" not in code:
    code = code.replace("import psycopg", "import psycopg\n            from psycopg_pool import ConnectionPool")

# Update __init__
init_replacement = """
    def __init__(self, db_url: str, embed_fn=None, encryption=None) -> None:
        try:
            import psycopg
            from psycopg_pool import ConnectionPool
            from pgvector.psycopg import register_vector
        except ImportError as e:
            raise RuntimeError(
                "PostgresGMPBackend requires psycopg, psycopg_pool and pgvector"
            ) from e

        self._embed = embed_fn or _default_embedder()
        self._dim = int(np.asarray(self._embed("dimension probe")).shape[0])
        self._encryption = encryption
        self._db_url = db_url

        # Initialize connection pool
        self._pool = ConnectionPool(db_url, min_size=1, max_size=20)
        
        # Setup vector extension and types initially
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            register_vector(conn)
        
        # Ensure schema
        self._ensure_schema()
"""

# Context manager for tenant connection
tenant_cursor_code = """
    @contextmanager
    def _tenant_conn(self, tenant_id: str):
        with self._pool.connection() as conn:
            from pgvector.psycopg import register_vector
            register_vector(conn)
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL app.current_tenant = %s", (tenant_id,))
                    yield conn, cur
"""

# We'll just write a whole new version of postgres_gmp.py because it's safer than complex regexes.
