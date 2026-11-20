import json
import shutil
import subprocess
import time
from pathlib import Path

import jwt
import pytest

import db
from conftest import SECRET, make_token

ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"


# ─── health + schema ──────────────────────────────────────────────────────────

def test_health_is_public(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_schema_is_idempotent(client):
    db.ensure_schema()
    db.ensure_schema()
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'public'")
        assert cur.fetchone()["n"] >= 8


# ─── login cookie ─────────────────────────────────────────────────────────────

def test_requires_login(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/daily").status_code == 401
    assert client.get("/api/tours").status_code == 401


@pytest.mark.parametrize("token", [
    "not-a-jwt",
    make_token(secret="some-other-secret"),   # signed with a different JWT_SECRET
    make_token(lifetime=-10, age=1000),       # expired
])
def test_rejects_bad_tokens(client, token):
    client.cookies.set("token", token)
    assert client.get("/api/me").status_code == 401


def test_rejects_token_without_user_id(client):
    now = int(time.time())
    client.cookies.set("token", jwt.encode({"username": "x", "iat": now, "exp": now + 100}, SECRET, algorithm="HS256"))
    assert client.get("/api/me").status_code == 401


def test_bearer_header_is_not_a_login(client):
    r = client.get("/api/me", headers={"Authorization": f"Bearer {make_token()}"})
    assert r.status_code == 401


def test_me_creates_the_user_row(alice):
    r = alice.get("/api/me")
    assert r.status_code == 200
    assert r.json() == {"id": ALICE, "username": "alice", "has_garmin": False, "garmin_status": None}
    with db.cursor() as cur:
        cur.execute("SELECT username FROM users WHERE id = %s", (ALICE,))
        assert cur.fetchone()["username"] == "alice"


def test_sliding_session_renews_only_past_halfway(client):
    day = 86400
    # 10% through its life: nothing to renew
    client.cookies.set("token", make_token(lifetime=100 * day, age=10 * day))
    assert "token" not in client.get("/api/me").cookies

    # 60% through: a fresh cookie with a full new window, same claims
    client.cookies.set("token", make_token(lifetime=100 * day, age=60 * day))
    r = client.get("/api/me")
    renewed = r.cookies.get("token")
    assert renewed
    claims = jwt.decode(renewed, SECRET, algorithms=["HS256"])
    assert claims["id"] == ALICE and claims["sid"] == "s1" and claims["role"] == "user"
    assert 99 * day < claims["exp"] - claims["iat"] <= 100 * day
    assert claims["iat"] >= time.time() - 5


def test_accepts_a_cookie_minted_by_the_real_quarc_auth(client):
    """Cross-implementation check: sign with the Node `jsonwebtoken` quarc-auth itself uses."""
    node = shutil.which("node")
    auth_dir = Path(__file__).resolve().parents[3] / "Quarc_Store" / "auth"
    if not node or not (auth_dir / "node_modules" / "jsonwebtoken").exists():
        pytest.skip("Quarc_Store/auth with node_modules not available")
    script = (
        "const jwt=require('jsonwebtoken');"
        f"console.log(jwt.sign({{id:'{BOB}',username:'bob',role:'admin',sid:'abc'}},'{SECRET}',{{expiresIn:3600}}))"
    )
    token = subprocess.run([node, "-e", script], cwd=auth_dir, capture_output=True, text=True, check=True).stdout.strip()
    client.cookies.set("token", token)
    r = client.get("/api/me")
    assert r.status_code == 200 and r.json()["username"] == "bob"


# ─── Garmin credentials ───────────────────────────────────────────────────────

def test_garmin_credentials_are_write_only_and_encrypted(alice):
    assert alice.get("/api/me").json()["has_garmin"] is False
    r = alice.put("/api/garmin/credentials", json={"email": " me@example.com ", "password": "hunter2"})
    assert r.status_code == 200
    assert alice.get("/api/me").json()["has_garmin"] is True

    with db.cursor() as cur:
        cur.execute("SELECT garmin_email_enc, garmin_pass_enc FROM users WHERE id = %s", (ALICE,))
        row = cur.fetchone()
    assert b"hunter2" not in bytes(row["garmin_pass_enc"]) and b"me@example.com" not in bytes(row["garmin_email_enc"])
    from auth import decrypt_credential
    assert decrypt_credential(row["garmin_email_enc"]) == "me@example.com"   # trimmed
    assert decrypt_credential(row["garmin_pass_enc"]) == "hunter2"
    assert "hunter2" not in json.dumps(alice.get("/api/me").json())

    assert alice.delete("/api/garmin/credentials").status_code == 204
    assert alice.get("/api/me").json()["has_garmin"] is False


# ─── data is per user ─────────────────────────────────────────────────────────

def _seed(user_id, username, steps):
    with db.cursor() as cur:
        cur.execute("INSERT INTO users (id, username) VALUES (%s, %s) ON CONFLICT DO NOTHING", (user_id, username))
        cur.execute("INSERT INTO daily_summary (user_id, date, steps) VALUES (%s, CURRENT_DATE, %s)", (user_id, steps))
        cur.execute(
            "INSERT INTO activities (user_id, activity_id, start_time, activity_type, name, distance_meters, "
            "start_lat, start_lng, polyline, weather_data) VALUES (%s, 1, now(), 'RUNNING', %s, 5000, 48.1, 11.5, "
            "'[[48.1, 11.5, 3.0], [48.2, 11.6, 3.1]]', '{\"temperature_2m\": [10]}')",
            (user_id, f"{username}'s run"),
        )


def test_each_person_only_sees_their_own_data(client):
    _seed(ALICE, "alice", 1111)
    _seed(BOB, "bob", 2222)

    client.cookies.set("token", make_token(user_id=ALICE, username="alice"))
    assert [d["steps"] for d in client.get("/api/daily").json()] == [1111]
    acts = client.get("/api/activities").json()
    assert [a["name"] for a in acts] == ["alice's run"]
    assert client.get("/api/range").json()["latest"] is not None
    summary = client.get("/api/summary").json()
    assert summary["period_7d"]["daily"]["avg_steps"] == 1111
    touring = client.get("/api/touring").json()
    assert [a["name"] for a in touring["activities"]] == ["alice's run"]
    assert touring["activities"][0]["polyline"][0] == [48.1, 11.5, 3.0]   # jsonb comes back as real JSON

    client.cookies.set("token", make_token(user_id=BOB, username="bob"))
    assert [d["steps"] for d in client.get("/api/daily").json()] == [2222]
    assert [a["name"] for a in client.get("/api/activities/map").json()] == ["bob's run"]


# ─── tours ────────────────────────────────────────────────────────────────────

def test_tour_lifecycle_and_isolation(client):
    _seed(ALICE, "alice", 1)
    client.cookies.set("token", make_token(user_id=ALICE, username="alice"))

    created = client.post("/api/tours", json={"name": "Alps", "description": "summer", "activity_ids": [1]})
    assert created.status_code == 201
    tour_id = created.json()["id"]

    listed = client.get("/api/tours").json()
    assert [(t["name"], t["activity_count"]) for t in listed] == [("Alps", 1)]
    detail = client.get(f"/api/tours/{tour_id}").json()
    assert detail["tour"]["name"] == "Alps" and len(detail["activities"]) == 1

    assert client.put(f"/api/tours/{tour_id}", json={"name": "Alps 2025", "description": None}).status_code == 200
    assert client.get(f"/api/tours/{tour_id}").json()["tour"]["name"] == "Alps 2025"

    # someone else can neither read nor delete it
    client.cookies.set("token", make_token(user_id=BOB, username="bob"))
    assert client.get(f"/api/tours/{tour_id}").status_code == 404
    client.delete(f"/api/tours/{tour_id}")
    client.cookies.set("token", make_token(user_id=ALICE, username="alice"))
    assert client.get(f"/api/tours/{tour_id}").status_code == 200

    assert client.delete(f"/api/tours/{tour_id}").status_code == 204
    assert client.get(f"/api/tours/{tour_id}").status_code == 404


def test_deleting_a_user_removes_their_data(client):
    _seed(ALICE, "alice", 1)
    with db.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (ALICE,))
        cur.execute("SELECT (SELECT count(*) FROM daily_summary) + (SELECT count(*) FROM activities) AS n")
        assert cur.fetchone()["n"] == 0


# ─── radar tiles are public (Leaflet can't send the cookie for <img> in every setup) ──────────

def test_radar_tile_404_without_login_needed(client):
    assert client.get("/api/radar/1700000000/6/33/21").status_code == 404
