import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager


def get_conn():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "db"),
        port=os.getenv("POSTGRES_PORT", 5432),
        dbname=os.getenv("POSTGRES_DB", "sport"),
        user=os.getenv("POSTGRES_USER", "sport"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


@contextmanager
def cursor():
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    finally:
        conn.close()


# Same lock key as sync/schema.py: the api and sync containers both apply the
# schema on start-up, and two sessions running CREATE TABLE IF NOT EXISTS at the
# same moment can still collide — the lock makes the second one wait.
_SCHEMA_LOCK = 7351204


def ensure_schema(path: str | None = None) -> None:
    """Apply db/init.sql (every statement in it is idempotent)."""
    path = path or os.getenv("SCHEMA_FILE", "/app/init.sql")
    with open(path, encoding="utf-8") as f:
        sql = f.read()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_LOCK,))
            try:
                cur.execute(sql)
                conn.commit()
            finally:
                conn.rollback()
                cur.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_LOCK,))
                conn.commit()
    finally:
        conn.close()
