from datetime import date, timedelta

import pytest

import db
from conftest import make_token
from health_connect_api import activity_id_for

ALICE = "11111111-1111-1111-1111-111111111111"
TODAY = date.today()


@pytest.fixture()
def phone(client):
    client.cookies.set("token", make_token())
    return client


def day(n):
    return (TODAY - timedelta(days=n)).isoformat()


def upload(c, **body):
    return c.post("/api/health-connect/ingest", json=body)


def test_needs_login(client):
    assert client.post("/api/health-connect/ingest", json={}).status_code == 401
    assert client.get("/api/health-connect/status").status_code == 401
    assert client.delete("/api/health-connect").status_code == 401


def test_upload_stores_only_what_the_phone_sent_as_its_own_source(phone):
    r = upload(
        phone, device="Pixel Tablet",
        daily=[{"date": day(1), "steps": 8000, "distance_meters": 6100.5, "resting_hr": 58}],
        sleep=[{"date": day(1), "start_time": "2026-11-19T22:30:00Z", "end_time": "2026-11-20T06:30:00Z",
                "duration_seconds": 26000, "light_seconds": 14000, "deep_seconds": 6000, "rem_seconds": 6000, "awake_seconds": 2400}],
        activities=[{"record_id": "abc-1", "start_time": "2026-11-19T17:00:00Z", "activity_type": "Running",
                     "duration_seconds": 1800, "distance_meters": 5000, "avg_hr": 150}],
    )
    assert r.status_code == 200 and r.json() == {"daily": 1, "sleep": 1, "activities": 1}

    src = phone.get("/api/sources").json()
    assert [(s["source"], s["connected"], s["has_data"]) for s in src["sources"]] == [("health_connect", True, True)]
    avail = src["sources"][0]["available"]
    assert set(avail["daily_summary"]) == {"steps", "distance_meters", "resting_hr"}      # nothing else is offered
    assert "body_battery_high" not in str(avail) and "sleep_score" not in str(avail)
    assert {"duration_seconds", "deep_seconds", "start_time"} <= set(avail["sleep"])
    assert set(avail["activities"]) == {"distance_meters", "avg_hr"}

    d = phone.get(f"/api/daily?source=health_connect&start={day(3)}&end={day(0)}").json()
    assert [(x["steps"], x["resting_hr"], x["stress_avg"]) for x in d] == [(8000, 58, None)]
    a = phone.get(f"/api/activities?source=health_connect&start={day(3)}&end={day(0)}").json()
    assert a[0]["activity_type"] == "RUNNING" and a[0]["name"] is None
    # and Garmin/Strava see none of it
    assert phone.get(f"/api/daily?source=garmin&start={day(3)}&end={day(0)}").json() == []

    st = phone.get("/api/health-connect/status").json()
    assert st["connected"] and st["device"] == "Pixel Tablet" and st["counts"] == {"daily": 1, "sleep": 1, "activities": 1}


def test_uploading_again_updates_instead_of_duplicating(phone):
    first = {"daily": [{"date": day(1), "steps": 100, "resting_hr": 60}],
             "activities": [{"record_id": "same", "start_time": "2026-11-19T17:00:00Z", "duration_seconds": 60, "distance_meters": 100}]}
    assert upload(phone, **first).status_code == 200
    second = {"daily": [{"date": day(1), "steps": 250}],       # no resting_hr this time: the old value stays
              "activities": [{"record_id": "same", "start_time": "2026-11-19T17:00:00Z", "duration_seconds": 90, "distance_meters": 140}]}
    assert upload(phone, **second).status_code == 200
    d = phone.get(f"/api/daily?source=health_connect&start={day(2)}&end={day(0)}").json()
    assert [(x["steps"], x["resting_hr"]) for x in d] == [(250, 60)]
    with db.cursor() as cur:
        cur.execute("SELECT duration_seconds, distance_meters FROM activities WHERE source = 'health_connect'")
        assert cur.fetchall() == [{"duration_seconds": 90, "distance_meters": 140.0}]


def test_activity_ids_are_stable_distinct_and_fit_the_column():
    a, b = activity_id_for("uuid-1"), activity_id_for("uuid-2")
    assert a == activity_id_for("uuid-1") and a != b
    assert 0 <= a < 2**63 and 0 <= b < 2**63


def test_the_same_day_twice_in_one_upload_is_fine(phone):
    r = upload(phone, daily=[{"date": day(1), "steps": 1}, {"date": day(1), "steps": 2}])
    assert r.status_code == 200 and r.json()["daily"] == 1
    d = phone.get(f"/api/daily?source=health_connect&start={day(2)}&end={day(0)}").json()
    assert [x["steps"] for x in d] == [2]


@pytest.mark.parametrize("body", [
    {"daily": [{"date": "1999-01-01", "steps": 1}]},                      # before anything could be recorded
    {"daily": [{"date": (TODAY + timedelta(days=30)).isoformat(), "steps": 1}]},
    {"daily": [{"date": day(1), "steps": -5}]},
    {"daily": [{"date": day(1), "steps": "many"}]},
    {"activities": [{"record_id": "", "start_time": "2026-11-19T17:00:00Z"}]},
    {"activities": [{"record_id": "x"}]},                                 # no start time
    {"daily": [{"date": day(1), "steps": 1}] * 801},                      # too many rows in one go
])
def test_nonsense_is_refused_and_nothing_is_stored(phone, body):
    assert upload(phone, **body).status_code == 422
    assert phone.get("/api/health-connect/status").json()["counts"] == {"daily": 0, "sleep": 0, "activities": 0}


def test_activity_type_is_tidied(phone):
    upload(phone, activities=[{"record_id": "t", "start_time": "2026-11-19T17:00:00Z", "activity_type": "trail run!"}])
    with db.cursor() as cur:
        cur.execute("SELECT activity_type FROM activities WHERE source = 'health_connect'")
        assert cur.fetchone()["activity_type"] == "TRAIL_RUN_"


def test_disconnect_deletes_only_the_phones_data(phone):
    upload(phone, daily=[{"date": day(1), "steps": 5}],
           activities=[{"record_id": "x", "start_time": "2026-11-19T17:00:00Z"}])
    with db.cursor() as cur:
        cur.execute("INSERT INTO daily_summary (user_id, source, date, steps) VALUES (%s, 'garmin', %s, 99)", (ALICE, day(1)))
    assert phone.delete("/api/health-connect").status_code == 204
    st = phone.get("/api/health-connect/status").json()
    assert st["connected"] is False and st["counts"] == {"daily": 0, "sleep": 0, "activities": 0}
    g = phone.get(f"/api/daily?source=garmin&start={day(2)}&end={day(0)}").json()
    assert [x["steps"] for x in g] == [99]


def test_two_people_do_not_see_each_others_phone_data(client):
    client.cookies.set("token", make_token())
    upload(client, daily=[{"date": day(1), "steps": 1}])
    client.cookies.set("token", make_token(user_id="22222222-2222-2222-2222-222222222222", username="bob"))
    assert client.get("/api/sources").json()["sources"] == []
    assert client.get("/api/health-connect/status").json()["counts"]["daily"] == 0
