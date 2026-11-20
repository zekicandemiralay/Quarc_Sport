"""
Sync service: who goes first, and the import itself (latest month first, then
older history newest-first) against a real PostgreSQL with a fake Garmin.

Same throwaway database as the API tests (see api/tests/conftest.py), from the
sync/ folder:

  pip install -r requirements.txt pytest
  python -m pytest tests
"""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest
from cryptography.fernet import Fernet

SYNC_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SYNC_DIR))
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "55432")
os.environ.setdefault("POSTGRES_DB", "sport")
os.environ.setdefault("POSTGRES_USER", "sport")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ["CREDENTIALS_KEY"] = Fernet.generate_key().decode()

import sync  # noqa: E402

REAL_LOGIN_USER = sync.login_user   # the pg fixture swaps in a fake

UTC = timezone.utc
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
TODAY = date.today()


# ─── next_job: pure ───────────────────────────────────────────────────────────

def cand(id, **kw):
    base = {"id": id, "sync_requested_at": None, "last_sync_at": NOW, "synced_until": TODAY,
            "history_from": None, "history_target": None, "gps_pending": False}
    base.update(kw)
    return base


def test_someone_who_just_connected_goes_first():
    busy = cand("a", history_from=TODAY - timedelta(days=60), history_target=TODAY - timedelta(days=3650))
    new = cand("b", sync_requested_at=NOW, last_sync_at=None, synced_until=None)
    assert sync.next_job([busy, new], NOW) == (new, "recent")


def test_oldest_request_first():
    a = cand("a", sync_requested_at=NOW)
    b = cand("b", sync_requested_at=NOW - timedelta(minutes=1))
    assert sync.next_job([a, b], NOW)[0]["id"] == "b"


def test_hourly_update_before_history():
    stale = cand("a", last_sync_at=NOW - timedelta(seconds=sync.INTERVAL + 1))
    history = cand("b", history_from=TODAY - timedelta(days=30), history_target=TODAY - timedelta(days=90))
    assert sync.next_job([history, stale], NOW) == (stale, "recent")
    assert sync.next_job([history], NOW) == (history, "history")


def test_history_goes_to_whoever_has_least_so_far():
    far = cand("a", history_from=TODAY - timedelta(days=2000), history_target=TODAY - timedelta(days=3650))
    near = cand("b", history_from=TODAY - timedelta(days=60), history_target=TODAY - timedelta(days=3650))
    assert sync.next_job([far, near], NOW)[0]["id"] == "b"


def test_routes_last_and_backed_off_when_stuck():
    r = cand("a", gps_pending=True)
    assert sync.next_job([r], NOW) == (r, "gps")
    assert sync.next_job([r], NOW, {"a": NOW.timestamp() + 60}) is None
    assert sync.next_job([cand("b")], NOW) is None


def test_first_import_plan():
    start, target = sync.first_import_plan(TODAY, None)
    assert start == TODAY - timedelta(days=sync.RECENT_DAYS - 1)
    assert target == TODAY - timedelta(days=sync.BACKFILL_DAYS)
    # an earlier version imported up to 200 days ago: only the gap since then is history
    start, target = sync.first_import_plan(TODAY, TODAY - timedelta(days=200))
    assert target == TODAY - timedelta(days=199)
    # ...or everything already: nothing older to fetch
    start, target = sync.first_import_plan(TODAY, TODAY - timedelta(days=2))
    assert target == start


# ─── the jobs, against PostgreSQL ─────────────────────────────────────────────

class FakeGarmin:
    def __init__(self):
        self.days = []

    def get_user_summary(self, ds):
        self.days.append(ds)
        return {"totalSteps": 1000 + len(self.days)}

    def get_sleep_data(self, ds):
        return {}

    def get_hrv_data(self, ds):
        return None

    def get_activities_by_date(self, start, end):
        return []


@pytest.fixture()
def pg(tmp_path, monkeypatch):
    try:
        psycopg2.connect(host=os.environ["POSTGRES_HOST"], port=os.environ["POSTGRES_PORT"],
                         dbname=os.environ["POSTGRES_DB"], user=os.environ["POSTGRES_USER"],
                         password=os.environ["POSTGRES_PASSWORD"], connect_timeout=3).close()
    except psycopg2.OperationalError:
        pytest.skip("no PostgreSQL reachable (see api/tests/conftest.py)")
    sync.db.ensure_schema(str(SYNC_DIR.parent / "db" / "init.sql"))
    sync._execute("TRUNCATE users RESTART IDENTITY CASCADE")
    monkeypatch.setattr(sync, "TOKEN_ROOT", tmp_path)
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    monkeypatch.setattr(sync, "BACKFILL_DAYS", 90)
    fake = FakeGarmin()
    monkeypatch.setattr(sync, "login_user", lambda user: fake)
    return fake


def add_user(uid, requested=True):
    f = Fernet(os.environ["CREDENTIALS_KEY"].encode())
    sync._execute(
        "INSERT INTO users (id, username, garmin_email_enc, garmin_pass_enc, garmin_status, sync_requested_at) "
        "VALUES (%s, %s, %s, %s, 'connected', CASE WHEN %s THEN now() END)",
        (uid, uid, f.encrypt(b"me@example.com"), f.encrypt(b"pw"), requested),
    )


def row(uid):
    conn = sync.db.get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT synced_until, history_from, history_target, last_sync_at, sync_requested_at, "
                    "syncing_since FROM users WHERE id = %s", (uid,))
        r = cur.fetchone()
    conn.close()
    return dict(zip(("synced_until", "history_from", "history_target", "last_sync_at", "requested", "syncing"), r))


def step():
    user, kind = sync.next_job(sync.load_candidates(), datetime.now(UTC))
    sync.run_job(user, kind)
    return user["id"], kind


def test_new_person_gets_latest_month_first_then_history_newest_first(pg):
    add_user("u1")
    assert step() == ("u1", "recent")
    r = row("u1")
    assert pg.days[0] == str(TODAY - timedelta(days=29)) and pg.days[-1] == str(TODAY)
    assert len(pg.days) == 30
    assert r["synced_until"] == TODAY and r["history_from"] == TODAY - timedelta(days=29)
    assert r["history_target"] == TODAY - timedelta(days=90)
    assert r["last_sync_at"] is not None and r["requested"] is None and r["syncing"] is None

    pg.days.clear()
    assert step() == ("u1", "history")
    assert pg.days[-1] == str(TODAY - timedelta(days=30))           # right before what we had
    assert row("u1")["history_from"] == TODAY - timedelta(days=59)

    step()
    assert row("u1")["history_from"] == TODAY - timedelta(days=89)
    step()                                                          # last, shorter piece
    assert row("u1")["history_from"] == TODAY - timedelta(days=90)
    assert sync.next_job(sync.load_candidates(), datetime.now(UTC)) is None   # all done


def test_a_new_request_jumps_ahead_of_someone_elses_history(pg):
    add_user("u1")
    step()                                                          # u1: recent; history pending
    add_user("u2")
    assert step() == ("u2", "recent")


def test_people_who_need_to_act_are_skipped(pg):
    add_user("u1")
    sync._execute("UPDATE users SET garmin_status = 'needs_code' WHERE id = 'u1'")
    assert sync.load_candidates() == []
    sync._execute("UPDATE users SET garmin_status = 'error', retry_after = now() + interval '10 minutes' WHERE id = 'u1'")
    assert sync.load_candidates() == []
    sync._execute("UPDATE users SET retry_after = now() - interval '1 minute' WHERE id = 'u1'")
    assert [c["id"] for c in sync.load_candidates()] == ["u1"]


def test_garmin_failures_become_a_status_the_app_shows(pg, monkeypatch):
    add_user("u1")
    user = {"id": "u1", "email": "me@example.com", "password": "pw"}

    def fails_with(exc):
        def _login(email, password, token_dir):
            raise exc
        return _login

    monkeypatch.setattr(sync, "_garmin_login", fails_with(sync.GarminConnectAuthenticationError("401")))
    assert REAL_LOGIN_USER(user) is None
    assert _status("u1") == ("auth_failed", False)

    monkeypatch.setattr(sync, "_garmin_login", fails_with(sync.GarminConnectTooManyRequestsError("429")))
    assert REAL_LOGIN_USER(user) is None
    assert _status("u1") == ("error", True)            # retried later by itself

    monkeypatch.setattr(sync, "_garmin_login", lambda e, p, d: (None, True))
    assert REAL_LOGIN_USER(user) is None
    assert _status("u1") == ("needs_code", False)

    monkeypatch.setattr(sync, "_garmin_login", lambda e, p, d: (object(), False))
    assert REAL_LOGIN_USER(user) is not None
    assert _status("u1") == ("connected", False)


def _status(uid):
    conn = sync.db.get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT garmin_status, retry_after IS NOT NULL FROM users WHERE id = %s", (uid,))
        r = cur.fetchone()
    conn.close()
    return r


def test_an_older_versions_import_counts_as_done(pg):
    add_user("u1")
    # imported oldest → newest by 1.0.0, stopped 40 days ago
    sync._execute("INSERT INTO daily_summary (user_id, date, steps) VALUES ('u1', %s, 5)", (TODAY - timedelta(days=40),))
    step()
    r = row("u1")
    assert r["history_target"] == TODAY - timedelta(days=39)       # only the gap is history now
    pg.days.clear()
    assert step() == ("u1", "history")
    assert pg.days[0] == str(TODAY - timedelta(days=39)) and pg.days[-1] == str(TODAY - timedelta(days=30))
    assert sync.next_job(sync.load_candidates(), datetime.now(UTC)) is None
