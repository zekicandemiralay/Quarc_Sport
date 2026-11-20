import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import jwt
import pytest

import db
import strava_api
from auth import decrypt_credential
from conftest import SECRET, make_token

ALICE = "11111111-1111-1111-1111-111111111111"


class FakeStravaOAuth:
    """Strava's token and deauthorize endpoints."""

    def __init__(self):
        self.athlete = {"id": 777, "firstname": "Ada", "lastname": "Runner"}
        self.calls = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()).items()}
                outer.calls.append((urlparse(self.path).path, form))
                if self.path.endswith("/token"):
                    ok = form.get("code") == "GOOD"
                    body = {"access_token": "acc", "refresh_token": "ref", "expires_at": int(time.time()) + 21600,
                            "athlete": outer.athlete} if ok else {"message": "Bad Request"}
                    self.send_response(200 if ok else 400)
                else:
                    body = {}
                    self.send_response(200)
                data = json.dumps(body).encode()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def strava(client, monkeypatch):
    fake = FakeStravaOAuth()
    monkeypatch.setattr(strava_api, "OAUTH_BASE", fake.base)
    monkeypatch.setenv("STRAVA_CLIENT_ID", "12345")
    monkeypatch.setenv("STRAVA_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("PUBLIC_URL", "https://sport.example:4007")
    client.cookies.set("token", make_token())
    yield client, fake
    fake.close()


def _state(client) -> str:
    url = client.post("/api/strava/authorize").json()["url"]
    return parse_qs(urlparse(url).query)["state"][0]


def _connection(user_id=ALICE):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM source_connections WHERE user_id = %s AND source = 'strava'", (user_id,))
        return cur.fetchone()


def _callback(client, state, *, code="GOOD", scope="read,activity:read_all", **extra):
    client.cookies.clear()          # Strava's redirect may arrive in another browser: no login cookie
    params = {"code": code, "state": state, "scope": scope, **extra}
    return client.get("/api/strava/callback", params=params)


# ─── status + authorize ───────────────────────────────────────────────────────

def test_not_set_up_on_this_server(client, monkeypatch):
    monkeypatch.delenv("STRAVA_CLIENT_ID", raising=False)
    monkeypatch.delenv("STRAVA_CLIENT_SECRET", raising=False)
    client.cookies.set("token", make_token())
    st = client.get("/api/strava/status").json()
    assert st["configured"] is False and st["connected"] is False and st["status"] == "not_connected"
    assert client.post("/api/strava/authorize").json()["detail"] == "not_configured"


def test_needs_login(client):
    assert client.get("/api/strava/status").status_code == 401
    assert client.post("/api/strava/authorize").status_code == 401
    assert client.delete("/api/strava").status_code == 401


def test_authorize_builds_the_strava_address(strava):
    client, _ = strava
    assert client.get("/api/strava/status").json()["configured"] is True
    url = client.post("/api/strava/authorize").json()["url"]
    q = parse_qs(urlparse(url).query)
    assert urlparse(url).path.endswith("/authorize")
    assert q["client_id"] == ["12345"] and q["response_type"] == ["code"]
    assert q["redirect_uri"] == ["https://sport.example:4007/api/strava/callback"]
    assert q["scope"] == ["read,activity:read_all"]
    claims = jwt.decode(q["state"][0], SECRET, algorithms=["HS256"])
    assert claims["sub"] == ALICE and claims["purpose"] == "strava_oauth"


# ─── the way back from Strava ─────────────────────────────────────────────────

def test_connecting_stores_encrypted_tokens_and_asks_for_an_import(strava):
    client, fake = strava
    page = _callback(client, _state(client))
    assert page.status_code == 200 and "Strava is connected" in page.text

    row = _connection()
    assert row["status"] == "connected" and row["display_name"] == "Ada Runner" and row["external_id"] == "777"
    assert row["sync_requested_at"] is not None and row["history_done"] is False
    assert b"acc" not in bytes(row["credentials_enc"])                       # encrypted at rest
    assert json.loads(decrypt_credential(row["credentials_enc"]))["refresh_token"] == "ref"
    assert fake.calls[0][1]["client_secret"] == "s3cret" and fake.calls[0][1]["grant_type"] == "authorization_code"

    client.cookies.set("token", make_token())
    st = client.get("/api/strava/status").json()
    assert st["connected"] and st["athlete"] == "Ada Runner" and st["queued"] is True and st["history_done"] is False
    assert "access_token" not in json.dumps(st)                             # tokens never leave the server
    assert [(s["source"], s["connected"]) for s in client.get("/api/sources").json()["sources"]] == [("strava", True)]


@pytest.mark.parametrize("make_state", [
    lambda c: "garbage",
    lambda c: jwt.encode({"sub": ALICE, "purpose": "strava_oauth", "exp": int(time.time()) - 5}, SECRET, algorithm="HS256"),
    lambda c: jwt.encode({"sub": ALICE, "purpose": "strava_oauth", "exp": int(time.time()) + 99}, "other-secret", algorithm="HS256"),
    lambda c: make_token(),                       # a login cookie is not an OAuth state
])
def test_callback_rejects_bad_states(strava, make_state):
    client, fake = strava
    assert _callback(client, make_state(client)).status_code == 400
    assert _connection() is None and fake.calls == []                       # nothing was exchanged


def test_activity_access_must_be_granted(strava):
    client, _ = strava
    assert _callback(client, _state(client), scope="read").status_code == 400
    assert _connection() is None
    client.cookies.set("token", make_token())        # _callback() leaves the browser logged out, like Strava's redirect
    assert _callback(client, _state(client), scope="read,activity:read").status_code == 200   # public activities only is fine
    assert _connection() is not None


def test_saying_no_connects_nothing(strava):
    client, _ = strava
    r = client.get("/api/strava/callback", params={"error": "access_denied", "state": _state(client)})
    assert r.status_code == 400 and _connection() is None


def test_a_refused_code_connects_nothing(strava):
    client, _ = strava
    assert _callback(client, _state(client), code="BAD").status_code == 502
    assert _connection() is None


def test_reconnecting_the_same_athlete_keeps_the_import_progress(strava):
    client, _ = strava
    _callback(client, _state(client))
    with db.cursor() as cur:
        cur.execute("UPDATE source_connections SET history_done = TRUE, history_page = 4, items_synced = 321, status = 'auth_failed' "
                    "WHERE user_id = %s AND source = 'strava'", (ALICE,))
    client.cookies.set("token", make_token())
    _callback(client, _state(client))
    row = _connection()
    assert row["status"] == "connected" and row["history_done"] is True and row["items_synced"] == 321


def test_a_different_strava_account_starts_over_and_drops_the_old_activities(strava):
    client, fake = strava
    _callback(client, _state(client))
    with db.cursor() as cur:
        cur.execute("INSERT INTO activities (user_id, source, activity_id, start_time, name) VALUES (%s, 'strava', 1, now(), 'old athlete')", (ALICE,))
        cur.execute("INSERT INTO activities (user_id, source, activity_id, start_time, name) VALUES (%s, 'garmin', 1, now(), 'garmin run')", (ALICE,))
        cur.execute("UPDATE source_connections SET history_done = TRUE WHERE user_id = %s AND source = 'strava'", (ALICE,))
    fake.athlete = {"id": 999, "firstname": "Bob", "lastname": "Other"}
    client.cookies.set("token", make_token())
    _callback(client, _state(client))
    row = _connection()
    assert row["external_id"] == "999" and row["history_done"] is False
    with db.cursor() as cur:
        cur.execute("SELECT source, name FROM activities WHERE user_id = %s", (ALICE,))
        assert cur.fetchall() == [{"source": "garmin", "name": "garmin run"}]


# ─── sync now / disconnect ────────────────────────────────────────────────────

def test_sync_now_queues_a_sync_only_when_connected(strava):
    client, _ = strava
    assert client.post("/api/strava/sync").status_code == 400
    _callback(client, _state(client))
    with db.cursor() as cur:
        cur.execute("UPDATE source_connections SET sync_requested_at = NULL WHERE user_id = %s AND source = 'strava'", (ALICE,))
    client.cookies.set("token", make_token())
    assert client.post("/api/strava/sync").json()["queued"] is True


def test_disconnect_forgets_tokens_and_deletes_only_strava_data(strava):
    client, fake = strava
    _callback(client, _state(client))
    with db.cursor() as cur:
        cur.execute("INSERT INTO activities (user_id, source, activity_id, start_time, name) VALUES (%s, 'strava', 5, now(), 's')", (ALICE,))
        cur.execute("INSERT INTO activities (user_id, source, activity_id, start_time, name) VALUES (%s, 'garmin', 5, now(), 'g')", (ALICE,))
        cur.execute("INSERT INTO daily_summary (user_id, source, date, steps) VALUES (%s, 'garmin', CURRENT_DATE, 10)", (ALICE,))
        cur.execute("INSERT INTO tours (user_id, source, name) VALUES (%s, 'strava', 'strava tour') RETURNING id", (ALICE,))
        tour = cur.fetchone()["id"]
        cur.execute("INSERT INTO tour_activities (tour_id, activity_id) VALUES (%s, 5)", (tour,))

    client.cookies.set("token", make_token())
    assert client.delete("/api/strava").status_code == 204
    assert any(path.endswith("/deauthorize") and form["access_token"] == "acc" for path, form in fake.calls)   # Strava told
    assert _connection() is None
    with db.cursor() as cur:
        cur.execute("SELECT source, name FROM activities WHERE user_id = %s", (ALICE,))
        assert cur.fetchall() == [{"source": "garmin", "name": "g"}]
        cur.execute("SELECT count(*) AS n FROM tours WHERE user_id = %s", (ALICE,))
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM tour_activities")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM daily_summary WHERE user_id = %s AND source = 'garmin'", (ALICE,))
        assert cur.fetchone()["n"] == 1                                       # Garmin's data is untouched
    assert client.get("/api/strava/status").json()["connected"] is False


def test_disconnect_works_even_when_strava_does_not_answer(strava, monkeypatch):
    client, fake = strava
    _callback(client, _state(client))
    monkeypatch.setattr(strava_api, "OAUTH_BASE", "http://127.0.0.1:9")       # nothing listens there
    client.cookies.set("token", make_token())
    assert client.delete("/api/strava").status_code == 204
    assert _connection() is None
