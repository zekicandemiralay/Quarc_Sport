import os
import re
from pathlib import Path

import psycopg2
import pytest

import db
from conftest import make_token

ALICE = "11111111-1111-1111-1111-111111111111"


def _seed_user():
    with db.cursor() as cur:
        cur.execute("INSERT INTO users (id, username) VALUES (%s, 'alice') ON CONFLICT DO NOTHING", (ALICE,))


def _daily(source, steps, **extra):
    cols = ["user_id", "source", "date", "steps"] + list(extra)
    vals = [ALICE, source, "2026-05-01", steps] + list(extra.values())
    with db.cursor() as cur:
        cur.execute(
            f"INSERT INTO daily_summary ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})", vals
        )


def _activity(source, activity_id, name, start="2026-05-01 08:00+00", **extra):
    cols = ["user_id", "source", "activity_id", "start_time", "activity_type", "name"] + list(extra)
    vals = [ALICE, source, activity_id, start, "RUNNING", name] + list(extra.values())
    with db.cursor() as cur:
        cur.execute(
            f"INSERT INTO activities ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})", vals
        )


@pytest.fixture()
def alice(client):
    _seed_user()
    client.cookies.set("token", make_token())
    return client


# ─── sources never mix ────────────────────────────────────────────────────────

def test_two_sources_can_hold_the_same_day_without_overwriting(alice):
    _daily("garmin", 1000, body_battery_high=90)
    _daily("strava", 2000)          # same user, same date, other source: must not collide

    g = alice.get("/api/daily?start=2026-05-01&end=2026-05-01&source=garmin").json()
    s = alice.get("/api/daily?start=2026-05-01&end=2026-05-01&source=strava").json()
    assert [r["steps"] for r in g] == [1000] and g[0]["body_battery_high"] == 90
    assert [r["steps"] for r in s] == [2000] and s[0]["body_battery_high"] is None


def test_activities_are_per_source(alice):
    _activity("garmin", 1, "garmin run")
    _activity("strava", 1, "strava run")   # same id in two sources is fine: the key includes the source

    names = lambda src: [a["name"] for a in alice.get(f"/api/activities?start=2026-05-01&end=2026-05-01&source={src}").json()]
    assert names("garmin") == ["garmin run"]
    assert names("strava") == ["strava run"]
    assert names("health_connect") == []


def test_unknown_source_is_rejected(alice):
    assert alice.get("/api/daily?source=fitbit").status_code == 400


def test_range_covers_activities_for_sources_without_daily_rows(alice):
    _activity("strava", 5, "ride", start="2026-03-10 07:00+00")
    r = alice.get("/api/range?source=strava").json()
    assert r["earliest"] == "2026-03-10" and r["latest"] == "2026-03-10"
    assert alice.get("/api/range?source=garmin").json() == {"earliest": None, "latest": None}


def test_tours_belong_to_one_source(alice):
    _activity("strava", 7, "ride")
    created = alice.post("/api/tours?source=strava", json={"name": "Strava tour", "description": None, "activity_ids": [7]})
    assert created.status_code == 201
    tour_id = created.json()["id"]

    assert [t["name"] for t in alice.get("/api/tours?source=strava").json()] == ["Strava tour"]
    assert alice.get("/api/tours?source=garmin").json() == []
    assert alice.get(f"/api/tours/{tour_id}?source=garmin").status_code == 404
    detail = alice.get(f"/api/tours/{tour_id}?source=strava").json()
    assert [a["name"] for a in detail["activities"]] == ["ride"]


# ─── /api/sources: what is there, nothing invented ────────────────────────────

def test_sources_lists_only_what_has_data_or_a_connection(alice):
    assert alice.get("/api/sources").json()["sources"] == []

    _daily("garmin", 1000, body_battery_high=90, resting_hr=50)
    _activity("strava", 1, "run", distance_meters=5000, avg_hr=150)
    body = alice.get("/api/sources").json()
    by = {s["source"]: s for s in body["sources"]}
    assert set(by) == {"garmin", "strava"}

    # Garmin has daily health data; Strava only activities
    assert {"steps", "body_battery_high", "resting_hr"} <= set(by["garmin"]["available"]["daily_summary"])
    assert "daily_summary" not in by["strava"]["available"]
    assert {"distance_meters", "avg_hr"} <= set(by["strava"]["available"]["activities"])
    assert "calories" not in by["strava"]["available"]["activities"]          # not reported, so not offered
    assert "body_battery_high" not in str(by["strava"]["available"])         # never derived for sources without it
    assert by["strava"]["connected"] is False and by["garmin"]["has_data"] is True


def test_connected_source_without_data_is_listed(alice):
    with db.cursor() as cur:
        cur.execute("INSERT INTO source_connections (user_id, source, status, credentials_enc) VALUES (%s, 'strava', 'connected', %s)",
                    (ALICE, psycopg2.Binary(b"tokens")))
    s = alice.get("/api/sources").json()["sources"]
    assert [(x["source"], x["connected"], x["has_data"]) for x in s] == [("strava", True, False)]


def test_a_strava_app_without_access_is_not_a_source_yet(alice):
    with db.cursor() as cur:
        cur.execute("INSERT INTO source_connections (user_id, source, status, app_credentials_enc) VALUES (%s, 'strava', 'pending', %s)",
                    (ALICE, psycopg2.Binary(b"app")))
    assert alice.get("/api/sources").json()["sources"] == []


def test_default_source_is_the_one_updated_last(alice):
    _daily("garmin", 1000)
    assert alice.get("/api/sources").json()["default"] == "garmin"
    _activity("strava", 1, "new strava run")
    assert alice.get("/api/sources").json()["default"] == "strava"
    # a data call without ?source= follows the same default
    names = [a["name"] for a in alice.get("/api/activities?start=2026-05-01&end=2026-05-01").json()]
    assert names == ["new strava run"]


def test_nobody_elses_sources_leak(alice):
    _daily("garmin", 1000)
    client = alice
    client.cookies.set("token", make_token(user_id="22222222-2222-2222-2222-222222222222", username="bob"))
    assert client.get("/api/sources").json()["sources"] == []
    assert client.get("/api/daily?start=2026-05-01&end=2026-05-01&source=garmin").json() == []


# ─── a database created before sources existed upgrades in place ──────────────

def _old_layout_ddl() -> str:
    """The current schema turned back into the pre-sources layout (no source column, old keys)."""
    sql = Path(os.environ["SCHEMA_FILE"]).read_text(encoding="utf-8")
    sql = re.sub(r"^\s+source\s+TEXT NOT NULL DEFAULT 'garmin',\n", "", sql, flags=re.M)
    sql = sql.replace("PRIMARY KEY (user_id, source, date)", "PRIMARY KEY (user_id, date)")
    sql = sql.replace("PRIMARY KEY (user_id, source, activity_id)", "PRIMARY KEY (user_id, activity_id)")
    sql = sql.replace("idx_activities_user_start ON activities(user_id, source, start_time)",
                      "idx_activities_user_start ON activities(user_id, start_time)")
    start, end = sql.index("DO $$"), sql.index("-- Connections to sources other than Garmin")
    sql = sql[:start] + sql[end:]
    sql = sql[:sql.index("CREATE TABLE IF NOT EXISTS source_connections")] + sql[sql.index("-- ERA5 weather grid"):]
    return sql


def test_old_layout_database_is_upgraded(db_available):
    admin = psycopg2.connect(
        host=os.environ["POSTGRES_HOST"], port=os.environ["POSTGRES_PORT"], dbname="postgres",
        user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
    )
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS upgrade_check")
        cur.execute("CREATE DATABASE upgrade_check")
    try:
        conn = psycopg2.connect(
            host=os.environ["POSTGRES_HOST"], port=os.environ["POSTGRES_PORT"], dbname="upgrade_check",
            user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(_old_layout_ddl())
        cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_name = 'daily_summary' AND column_name = 'source'")
        assert cur.fetchone()[0] == 0   # really the old layout

        cur.execute("INSERT INTO users (id, username) VALUES ('u1', 'old')")
        cur.execute("INSERT INTO daily_summary (user_id, date, steps) VALUES ('u1', '2026-01-01', 123)")
        cur.execute("INSERT INTO sleep (user_id, date, duration_seconds) VALUES ('u1', '2026-01-01', 25000)")
        cur.execute("INSERT INTO hrv (user_id, date, hrv_last_night) VALUES ('u1', '2026-01-01', 55)")
        cur.execute("INSERT INTO activities (user_id, activity_id, start_time, name) VALUES ('u1', 9, '2026-01-01 10:00+00', 'old run')")
        cur.execute("INSERT INTO tours (user_id, name) VALUES ('u1', 'old tour')")

        for _ in range(2):   # applying it twice must be harmless
            cur.execute(Path(os.environ["SCHEMA_FILE"]).read_text(encoding="utf-8"))

        for table in ("daily_summary", "sleep", "hrv", "activities", "tours"):
            cur.execute(f"SELECT source FROM {table}")
            assert cur.fetchall() == [("garmin",)], table      # everything that existed was Garmin's
        # the keys now include the source: the same day from another source is allowed
        cur.execute("INSERT INTO daily_summary (user_id, source, date, steps) VALUES ('u1', 'strava', '2026-01-01', 999)")
        cur.execute("INSERT INTO activities (user_id, source, activity_id, start_time, name) VALUES ('u1', 'strava', 9, '2026-01-01 10:00+00', 'x')")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute("INSERT INTO daily_summary (user_id, source, date, steps) VALUES ('u1', 'garmin', '2026-01-01', 1)")
        conn.close()
    finally:
        with admin.cursor() as cur:
            cur.execute("DROP DATABASE IF EXISTS upgrade_check")
        admin.close()
