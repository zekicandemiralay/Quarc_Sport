import os
import time
from datetime import datetime, timedelta, timezone

import pytest

import strava
import sync
from fake_strava import FakeStrava, activity
from test_jobs import pg  # noqa: F401  (fixture: schema + clean tables)

UTC = timezone.utc


# ─── pure mapping ─────────────────────────────────────────────────────────────

def test_decode_polyline_reference_vector():
    # the worked example from Google's polyline documentation
    pts = strava.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert pts == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]


def test_map_activity_keeps_only_what_strava_reports():
    row = strava.map_activity(activity(1, id=5, name="Morning run", average_cadence=84.0, average_watts=None))
    assert row["activity_id"] == 5 and row["name"] == "Morning run" and row["activity_type"] == "RUNNING"
    assert row["start_time"].tzinfo is not None
    assert row["distance_meters"] == 5001.0 and row["duration_seconds"] == 1500
    assert (row["avg_hr"], row["max_hr"]) == (150, 171)
    assert (row["start_lat"], row["start_lng"], row["end_lat"]) == (48.1, 11.5, 48.2)
    assert row["avg_pace_sec_per_km"] == round(1500 / 5.001, 1)
    assert row["avg_cadence"] == 84 and row["avg_power"] is None
    assert len(row["polyline"]) == 3 and row["polyline"][0] == [38.5, -120.2]
    assert "calories" not in row and "aerobic_te" not in row          # Strava's list doesn't have them: not invented


def test_activities_without_gps_or_distance():
    row = strava.map_activity(activity(2, sport_type="WeightTraining", type="WeightTraining", distance=0,
                                       start_latlng=[], end_latlng=[], map={"summary_polyline": ""},
                                       average_speed=0, average_heartrate=None, max_heartrate=None))
    assert row["activity_type"] == "STRENGTH_TRAINING"
    assert row["distance_meters"] is None and row["avg_pace_sec_per_km"] is None and row["avg_speed_mps"] is None
    assert row["start_lat"] is None and row["polyline"] is None and row["avg_hr"] is None


@pytest.mark.parametrize("sport,expected", [
    ("Run", "RUNNING"), ("TrailRun", "TRAIL_RUNNING"), ("Ride", "CYCLING"), ("MountainBikeRide", "MOUNTAIN_BIKING"),
    ("GravelRide", "GRAVEL_CYCLING"), ("Swim", "SWIMMING"), ("Hike", "HIKING"), ("Walk", "WALKING"),
    ("AlpineSki", "ALPINE_SKI"), ("StandUpPaddling", "STAND_UP_PADDLING"), ("EBikeRide", "E_BIKE_RIDE"),
])
def test_type_mapping(sport, expected):
    assert strava.map_type({"sport_type": sport}) == expected


def test_broken_activity_is_skipped():
    assert strava.map_activity({"name": "no id"}) is None
    assert strava.map_activity({"id": 1}) is None


def test_long_routes_are_thinned_out():
    pts = [(i / 1000, i / 1000) for i in range(5000)]
    thin = strava.downsample(pts)
    assert len(thin) <= strava.MAX_POLYLINE_POINTS + 1 and thin[0] == pts[0] and thin[-1] == pts[-1]


# ─── who goes first (pure) ────────────────────────────────────────────────────

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)


def cand(uid, **kw):
    base = {"user_id": uid, "sync_requested_at": None, "last_sync_at": NOW, "history_done": True}
    base.update(kw)
    return base


def test_next_job_priorities():
    assert strava.next_job([], NOW) is None
    assert strava.next_job([cand("a")], NOW) is None                                     # fresh and done
    assert strava.next_job([cand("a", last_sync_at=NOW - timedelta(hours=1))], NOW)["user_id"] == "a"   # due
    importing = cand("b", history_done=False, last_sync_at=NOW)
    due = cand("c", last_sync_at=NOW - timedelta(hours=2))
    assert strava.next_job([due, importing], NOW)["user_id"] == "b"                      # backlog before routine
    asked = cand("d", sync_requested_at=NOW - timedelta(seconds=5))
    assert strava.next_job([due, importing, asked], NOW)["user_id"] == "d"               # "sync now" first
    waited = cand("e", history_done=False, last_sync_at=NOW - timedelta(minutes=5))
    assert strava.next_job([importing, waited], NOW)["user_id"] == "e"                   # whoever waited longest


# ─── the job against a fake Strava and a real database ────────────────────────

@pytest.fixture()
def fake(monkeypatch):
    f = FakeStrava()
    monkeypatch.setattr(strava, "API_BASE", f.base + "/api/v3")
    monkeypatch.setattr(strava, "OAUTH_BASE", f.base + "/oauth")
    yield f
    f.close()


MY_APP = {"client_id": "424242", "client_secret": "s" * 40}


def connect(uid="u1", *, expires_in=3600, access="access-1", refresh="refresh-1", app=MY_APP, **cols):
    sync._execute("INSERT INTO users (id, username) VALUES (%s, %s) ON CONFLICT DO NOTHING", (uid, uid))
    creds = strava.dump_credentials({"access_token": access, "refresh_token": refresh, "expires_at": int(time.time()) + expires_in})
    sync._execute(
        "INSERT INTO source_connections (user_id, source, status, credentials_enc, app_credentials_enc, sync_requested_at) "
        "VALUES (%s, 'strava', 'connected', %s, %s, now())", (uid, creds, strava.dump_credentials(app) if app else None))
    for k, v in cols.items():
        sync._execute(f"UPDATE source_connections SET {k} = %s WHERE user_id = %s AND source = 'strava'", (v, uid))


def conn_row(uid="u1"):
    c = sync.db.get_conn()
    with c.cursor() as cur:
        cur.execute("SELECT status, error, last_sync_at, retry_after, history_done, history_page, items_synced, "
                    "syncing_since, sync_requested_at, credentials_enc FROM source_connections WHERE user_id = %s AND source = 'strava'", (uid,))
        r = cur.fetchone()
    c.close()
    return dict(zip(("status", "error", "last_sync_at", "retry_after", "history_done", "history_page", "items_synced",
                     "syncing", "requested", "creds"), r))


def stored(uid="u1", source="strava"):
    c = sync.db.get_conn()
    with c.cursor() as cur:
        cur.execute("SELECT activity_id, name, source FROM activities WHERE user_id = %s AND source = %s ORDER BY activity_id", (uid, source))
        rows = cur.fetchall()
    c.close()
    return rows


def run_next():
    job = strava.next_job(strava.load_candidates(), datetime.now(UTC))
    assert job is not None
    strava.run_job(job)
    return job


def test_first_import_reads_everything_and_marks_history_done(pg, fake):
    fake.activities = [activity(n) for n in range(1, 251)]       # 250 activities → 3 pages
    connect()
    run_next()

    r = conn_row()
    assert r["history_done"] is True and r["items_synced"] == 250 and r["status"] == "connected"
    assert r["last_sync_at"] is not None and r["syncing"] is None and r["requested"] is None
    rows = stored()
    assert len(rows) == 250 and {x[2] for x in rows} == {"strava"}
    # nothing else was touched
    assert stored(source="garmin") == []


def test_import_goes_a_few_pages_at_a_time_and_picks_up_where_it_stopped(pg, fake, monkeypatch):
    monkeypatch.setattr(strava, "PAGES_PER_JOB", 1)
    fake.activities = [activity(n) for n in range(1, 251)]
    connect()
    run_next()
    r = conn_row()
    assert len(stored()) == 100 and r["history_done"] is False and r["history_page"] == 2 and r["items_synced"] == 100
    run_next(); run_next()
    r = conn_row()
    assert len(stored()) == 250 and r["history_done"] is True


def test_newest_activities_arrive_first(pg, fake, monkeypatch):
    monkeypatch.setattr(strava, "PAGES_PER_JOB", 1)
    fake.activities = [activity(n) for n in range(1, 151)]
    connect()
    run_next()
    ids = [x[0] for x in stored()]
    assert min(ids) == 1000 + 51 and max(ids) == 1000 + 150      # the newest 100 of 150


def test_later_syncs_pick_up_new_and_edited_activities(pg, fake):
    fake.activities = [activity(n) for n in range(1, 4)]
    connect()
    run_next()
    assert len(stored()) == 3

    fake.activities.append(activity(9, days_ago=0, name="Just now"))
    fake.activities[2]["name"] = "Renamed"                         # an older edit within the recent window
    fake.activities[2]["start_date"] = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sync._execute("UPDATE source_connections SET last_sync_at = now() - interval '2 hours' WHERE user_id = 'u1'")
    run_next()
    names = {x[0]: x[1] for x in stored()}
    assert names[1009] == "Just now" and names[1003] == "Renamed"
    assert "after" in fake.calls("/api/v3/athlete/activities")[-1]["query"]      # incremental, not a full re-read


def test_expired_token_is_refreshed_and_the_new_one_is_kept(pg, fake):
    fake.activities = [activity(1)]
    connect(expires_in=-10)                                        # already expired
    run_next()
    assert len(fake.calls("/oauth/token")) == 1
    refresh_form = fake.calls("/oauth/token")[0]["form"]
    assert (refresh_form["client_id"], refresh_form["client_secret"]) == (MY_APP["client_id"], MY_APP["client_secret"])   # the person's OWN app
    assert fake.calls("/api/v3/athlete/activities")[0]["auth"] == f"Bearer {fake.access_token}"
    creds = strava.load_credentials(conn_row()["creds"])
    assert creds["refresh_token"] == fake.refresh_token != "refresh-1"      # the rotated refresh token is saved
    assert conn_row()["status"] == "connected"


def test_revoked_access_needs_a_reconnect_and_is_not_retried(pg, fake):
    fake.fail_with = 401
    connect()
    run_next()
    r = conn_row()
    assert r["status"] == "auth_failed" and r["error"]
    assert strava.load_candidates() == []                           # left alone until the person reconnects


def test_two_people_refresh_with_their_own_apps(pg, fake):
    fake.activities = [activity(1)]
    connect("u1", expires_in=-10, app={"client_id": "111111", "client_secret": "x" * 40})
    run_next()
    sync._execute("UPDATE source_connections SET sync_requested_at = now() WHERE user_id = 'u1'")
    fake.refresh_token = "refresh-2"
    connect("u2", expires_in=-10, refresh="refresh-2", app={"client_id": "222222", "client_secret": "y" * 40})
    run_next(); run_next()
    ids = [c["form"]["client_id"] for c in fake.calls("/oauth/token")]
    assert sorted(set(ids)) == ["111111", "222222"]


def test_a_missing_app_needs_a_reconnect_instead_of_crashing(pg, fake):
    connect(expires_in=-10, app=None)                                  # tokens expired and no app to refresh them with
    run_next()
    r = conn_row()
    assert r["status"] == "auth_failed" and "app" in r["error"].lower()
    assert fake.calls("/oauth/token") == []


def test_refused_refresh_also_needs_a_reconnect(pg, fake):
    connect(expires_in=-10, refresh="stale-token")                  # Strava no longer knows this refresh token
    run_next()
    assert conn_row()["status"] == "auth_failed"


def test_rate_limit_waits_instead_of_failing(pg, fake):
    fake.fail_with = 429
    connect()
    run_next()
    r = conn_row()
    assert r["status"] == "connected" and r["retry_after"] is not None
    assert r["retry_after"] > datetime.now(UTC) + timedelta(minutes=10)
    assert strava.load_candidates() == []


def test_running_out_of_the_daily_budget_waits_until_tomorrow(pg, fake):
    fake.fail_with = 429
    fake.usage = (10, 1000)
    connect()
    run_next()
    assert conn_row()["retry_after"] > datetime.now(UTC) + timedelta(hours=1) or datetime.now(UTC).hour >= 23


def test_nearly_out_of_budget_stops_early(pg, fake):
    fake.activities = [activity(n) for n in range(1, 251)]
    fake.usage = (99, 500)                                          # 15-minute window almost used up
    connect()
    run_next()
    assert len(fake.calls("/api/v3/athlete/activities")) == 1       # one page, then it backs off
    r = conn_row()
    assert r["history_done"] is False and r["retry_after"] is not None


def test_strava_trouble_is_a_temporary_error(pg, fake):
    fake.fail_with = 500
    connect()
    run_next()
    r = conn_row()
    assert r["status"] == "error" and r["retry_after"] is not None


def test_strava_activities_get_countries_and_weather_through_the_shared_enrichment(pg, fake):
    seen = []
    fake.activities = [activity(1)]
    connect()
    job = strava.next_job(strava.load_candidates(), datetime.now(UTC))
    strava.run_job(job, enrich=lambda uid: seen.append(uid))
    assert seen == ["u1"]
