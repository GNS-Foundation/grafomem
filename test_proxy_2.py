from aml.cloud.db_pool import DatabasePool
import psycopg
pool = DatabasePool("postgresql://grafomem:dev@localhost:5432/grafomem")
conn = pool.getconn()
print(dir(conn))
