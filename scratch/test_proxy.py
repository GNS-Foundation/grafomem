import psycopg

class Proxy:
    def __init__(self, conn):
        self._conn = conn
    
    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        print("Proxy.__exit__ called")
        return False

with psycopg.connect("postgresql://grafomem:dev@localhost:5432/grafomem") as conn:
    proxy = Proxy(conn)
    with proxy as p:
        print("Inside proxy")
