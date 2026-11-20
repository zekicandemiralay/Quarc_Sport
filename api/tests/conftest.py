"""
API tests. They need a real PostgreSQL (the SQL is Postgres-specific), skipped if none is reachable.

Start a throwaway one and run the tests from the api/ folder:

  docker run -d --name sport-test-pg -e POSTGRES_USER=sport -e POSTGRES_PASSWORD=test \
      -e POSTGRES_DB=sport -p 55432:5432 postgres:16
  pip install -r requirements.txt pytest httpx
  python -m pytest tests

  docker rm -f sport-test-pg        # when done
"""

import os
import sys
import time
from pathlib import Path

import jwt
import psycopg2
import pytest
from cryptography.fernet import Fernet

API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))

# Set before the app modules are imported: they read these at import time.
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "55432")
os.environ.setdefault("POSTGRES_DB", "sport")
os.environ.setdefault("POSTGRES_USER", "sport")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ["JWT_SECRET"] = "test-shared-secret"
os.environ["CREDENTIALS_KEY"] = Fernet.generate_key().decode()
os.environ["SECURE_COOKIE"] = "false"
os.environ["SCHEMA_FILE"] = str(API_DIR.parent / "db" / "init.sql")

SECRET = os.environ["JWT_SECRET"]


def make_token(user_id="11111111-1111-1111-1111-111111111111", username="alice", role="user",
               lifetime=365 * 86400, age=0, secret=SECRET, **extra):
    """A login cookie value shaped like the ones quarc-auth issues (HS256; id, username, role, sid)."""
    now = int(time.time()) - age
    claims = {"id": user_id, "username": username, "role": role, "sid": "s1", "iat": now, "exp": now + lifetime}
    claims.update(extra)
    return jwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture(scope="session")
def db_available():
    try:
        psycopg2.connect(
            host=os.environ["POSTGRES_HOST"], port=os.environ["POSTGRES_PORT"], dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"], connect_timeout=3,
        ).close()
    except psycopg2.OperationalError:
        pytest.skip("no PostgreSQL reachable (see tests/conftest.py)")


@pytest.fixture()
def client(db_available):
    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as c:  # runs the lifespan: applies the schema
        _wipe()
        yield c


@pytest.fixture()
def alice(client):
    client.cookies.set("token", make_token())
    return client


def _wipe():
    import db
    with db.cursor() as cur:
        cur.execute("TRUNCATE users, tours, tour_activities RESTART IDENTITY CASCADE")
