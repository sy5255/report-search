import mysql.connector
from mysql.connector import pooling
from app.config import MYSQL_HOST, MYSQL_PORT, MYSQL_DB, MYSQL_USER, MYSQL_PASS

_pool = None

def init_pool():
    global _pool
    if _pool is not None:
        return
    _pool = pooling.MySQLConnectionPool(
        pool_name="ragchat_pool",
        pool_size=10,
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        database=MYSQL_DB,
        user=MYSQL_USER,
        password=MYSQL_PASS,
        autocommit=False,
    )

def get_conn():
    if _pool is None:
        init_pool()
    return _pool.get_connection()