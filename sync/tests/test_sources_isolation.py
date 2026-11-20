"""Other sources (Strava, Health Connect) must never disturb the Garmin sync, and vice versa."""

from datetime import date

import sync
from test_jobs import pg, add_user  # noqa: F401  (pg is a fixture)


def _q(sql, params=()):
    conn = sync.db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.commit()
        conn.close()


def test_other_sources_do_not_make_garmin_think_history_is_done(pg):
    add_user("u1", requested=False)
    _q("INSERT INTO daily_summary (user_id, source, date, steps) VALUES ('u1', 'health_connect', CURRENT_DATE, 5)")
    _q("INSERT INTO sleep (user_id, source, date, duration_seconds) VALUES ('u1', 'health_connect', CURRENT_DATE, 25000)")
    # Garmin has nothing yet, so its first import must still cover the full history
    assert sync.get_latest_synced_date("u1") is None
    assert sync.get_earliest_synced_date("u1") is None


def test_garmin_only_fetches_routes_for_garmin_activities(pg):
    add_user("u1", requested=False)
    # an outdoor Strava activity with a start point but no route yet: Garmin must not try to fetch it
    _q("INSERT INTO activities (user_id, source, activity_id, start_time, activity_type, start_lat, start_lng) "
       "VALUES ('u1', 'strava', 42, now(), 'RUNNING', 48.1, 11.5)")
    conn = sync.db.get_conn()
    assert sync.gps_pending(conn, "u1") == 0
    assert all(not c["gps_pending"] for c in sync.load_candidates())
    conn.close()


def test_a_garmin_sync_never_overwrites_another_sources_day(pg):
    add_user("u1", requested=False)
    today = date.today()
    _q("INSERT INTO daily_summary (user_id, source, date, steps) VALUES ('u1', 'strava', %s, 5)", (today,))
    conn = sync.db.get_conn()
    sync.db.upsert_daily_summary(conn, [{"date": today, "steps": 111}], "u1")
    sync.db.upsert_daily_summary(conn, [{"date": today, "steps": 222}], "u1")   # Garmin updates its own row again
    conn.close()
    rows = dict(_q("SELECT source, steps FROM daily_summary WHERE user_id = 'u1'"))
    assert rows == {"strava": 5, "garmin": 222}


def test_people_without_a_garmin_login_are_not_garmin_candidates(pg):
    add_user("garmin_person", requested=False)
    # connected to Strava only: has a users row and a connection, but no Garmin login
    _q("INSERT INTO users (id, username) VALUES ('strava_person', 'strava_person')")
    _q("INSERT INTO source_connections (user_id, source) VALUES ('strava_person', 'strava')")
    assert [c["id"] for c in sync.load_candidates()] == ["garmin_person"]
