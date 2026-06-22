from aml.cloud.db_pool import DatabasePool, _PooledConnectionProxy
pool = DatabasePool("postgresql://grafomem:dev@localhost:5432/grafomem")
conn = pool.getconn()
try:
    conn.autocommit = False
    print("Success! Proxy autocommit =", conn.autocommit)
except Exception as e:
    print("Error:", type(e), e)
