import os
import psycopg
import time
from psycopg_pool import ConnectionPool

db_url = os.environ.get("DATABASE_URL", "postgresql://grafomem:grafomem@localhost:5433/grafomem")
pool = ConnectionPool(db_url, kwargs={"autocommit": True})
pool.wait()

class Proxy:
    def __init__(self, conn):
        self._conn = conn
    
    def __getattr__(self, item):
        return getattr(self._conn, item)

    def __del__(self):
        pool.putconn(self._conn)

def _persist():
    conn = Proxy(pool.getconn())
    conn.execute("SELECT 1;")
    # conn goes out of scope, __del__ should be called.

def _increment():
    conn = Proxy(pool.getconn())
    conn.execute("SELECT 2;")
    print("Increment succeeded")

try:
    _persist()
    _increment()
except Exception as e:
    import traceback
    traceback.print_exc()
finally:
    pool.close()
