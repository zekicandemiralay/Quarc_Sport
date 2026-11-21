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
BOB = "22222222-2222-2222-2222-222222222222"
ALICE_APP = ("12345", "a" * 40)
BOB_APP = ("67890", "b" * 40)


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
    monkeypatch.setenv("PUBLIC_URL", "https://sport.example:4007")
    client.cookies.set("token", make_token())
    yield client, fake
    fake.close()


def _save_app(client, app=ALICE_APP):
    return client.put("/api/strava/app", json={"client_id": app[0], "client_secret": app[1]})


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


def _login(client, user_id=ALICE, username="alice"):
    client.cookies.set("token", make_token(user_id=user_id, username=username))


def _connect(client, app=ALICE_APP, user_id=ALICE, username="alice"):
    _login(client, user_id, username)
    assert _save_app(client, app).status_code == 200
    r = _callback(client, _state(client))
    assert r.status_code == 200
    _login(client, user_id, username)
    return r


# ─── status + the person's own app ────────────────────────────────────────────

def test_needs_login(client):
    assert client.get("/api/strava/status").status_code == 401
    assert client.put("/api/strava/app", json={"client_id": "1", "client_secret": "x"}).status_code == 401
    assert client.post("/api/strava/authorize").status_code == 401
    assert client.delete("/api/strava").status_code == 401


def test_status_tells_the_person_what_to_enter_into_strava(strava):
    client, _ = strava
    st = client.get("/api/strava/status").json()
    assert st["configured"] is True and st["connected"] is False and st["app_saved"] is False and st["status"] == "not_connected"
    assert st["website"] == "https://sport.example:4007" and st["callback_domain"] == "sport.example"


def test_without_the_servers_address_strava_cannot_be_started(client, monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    client.cookies.set("token", make_token())
    assert client.get("/api/strava/status").json()["configured"] is False
    _save_app(client)
    assert client.post("/api/strava/authorize").json()["detail"] == "no_public_url"


@pytest.mark.parametrize("cid,secret,code", [
    ("abc", "a" * 40, "invalid_client_id"), ("12", "a" * 40, "invalid_client_id"), ("", "a" * 40, "invalid_client_id"),
    ("12345", "short", "invalid_client_secret"), ("12345", "has spaces " + "a" * 30, "invalid_client_secret"), ("12345", "", "invalid_client_secret"),
])
def test_obvious_typos_are_caught_when_saving(strava, cid, secret, code):
    client, _ = strava
    r = client.put("/api/strava/app", json={"client_id": cid, "client_secret": secret})
    assert r.status_code == 400 and r.json()["detail"] == code
    assert _connection() is None


def test_the_app_is_stored_encrypted_and_the_secret_never_comes_back(strava):
    client, _ = strava
    assert _save_app(client, (" 12345 ", " " + "a" * 40 + " ")).status_code == 200     # stray spaces from copy & paste
    st = client.get("/api/strava/status").json()
    assert st["app_saved"] is True and st["client_id"] == "12345" and st["connected"] is False and st["status"] == "pending"
    assert "a" * 20 not in json.dumps(st)

    row = _connection()
    assert b"a" * 20 not in bytes(row["app_credentials_enc"])                       # encrypted at rest
    assert json.loads(decrypt_credential(row["app_credentials_enc"])) == {"client_id": "12345", "client_secret": "a" * 40}
    assert row["credentials_enc"] is None
    # an app without access is not a source yet
    assert client.get("/api/sources").json()["sources"] == []


def test_authorize_needs_the_persons_app_and_uses_it(strava):
    client, _ = strava
    assert client.post("/api/strava/authorize").json()["detail"] == "no_app"
    _save_app(client)
    url = client.post("/api/strava/authorize").json()["url"]
    q = parse_qs(urlparse(url).query)
    assert urlparse(url).path.endswith("/authorize")
    assert q["client_id"] == [ALICE_APP[0]] and q["response_type"] == ["code"]
    assert q["redirect_uri"] == ["https://sport.example:4007/api/strava/callback"]
    assert q["scope"] == ["read,activity:read_all"]
    claims = jwt.decode(q["state"][0], SECRET, algorithms=["HS256"])
    assert claims["sub"] == ALICE and claims["purpose"] == "strava_oauth"


def test_everyone_uses_their_own_app_never_someone_elses(strava):
    client, fake = strava
    _connect(client, ALICE_APP, ALICE, "alice")
    _connect(client, BOB_APP, BOB, "bob")

    # each exchange was made with that person's own Client ID and Secret
    exchanges = [form for path, form in fake.calls if path.endswith("/token")]
    assert [(f["client_id"], f["client_secret"]) for f in exchanges] == [ALICE_APP, BOB_APP]

    _login(client, BOB, "bob")
    assert parse_qs(urlparse(client.post("/api/strava/authorize").json()["url"]).query)["client_id"] == [BOB_APP[0]]
    _login(client, ALICE, "alice")
    assert client.get("/api/strava/status").json()["client_id"] == ALICE_APP[0]


# ─── the way back from Strava ─────────────────────────────────────────────────

def test_connecting_stores_encrypted_tokens_and_asks_for_an_import(strava):
    client, fake = strava
    _save_app(client)
    page = _callback(client, _state(client))
    assert page.status_code == 200 and "Strava is connected" in page.text

    row = _connection()
    assert row["status"] == "connected" and row["display_name"] == "Ada Runner" and row["external_id"] == "777"
    assert row["sync_requested_at"] is not None and row["history_done"] is False
    assert b"acc" not in bytes(row["credentials_enc"])                       # encrypted at rest
    assert json.loads(decrypt_credential(row["credentials_enc"]))["refresh_token"] == "ref"
    assert fake.calls[0][1]["client_secret"] == "a" * 40 and fake.calls[0][1]["grant_type"] == "authorization_code"

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
    _save_app(client)
    assert _callback(client, make_state(client)).status_code == 400
    assert _connection()["credentials_enc"] is None and fake.calls == []     # nothing was exchanged


def test_a_state_for_someone_without_an_app_does_nothing(strava):
    client, fake = strava
    state = jwt.encode({"sub": BOB, "purpose": "strava_oauth", "exp": int(time.time()) + 99}, SECRET, algorithm="HS256")
    assert _callback(client, state).status_code == 400 and fake.calls == []


def test_activity_access_must_be_granted(strava):
    client, _ = strava
    _save_app(client)
    assert _callback(client, _state(client), scope="read").status_code == 400
    assert _connection()["credentials_enc"] is None
    client.cookies.set("token", make_token())        # _callback() leaves the browser logged out, like Strava's redirect
    assert _callback(client, _state(client), scope="read,activity:read").status_code == 200   # public activities only is fine
    assert _connection()["credentials_enc"] is not None


def test_saying_no_connects_nothing(strava):
    client, _ = strava
    _save_app(client)
    r = client.get("/api/strava/callback", params={"error": "access_denied", "state": _state(client)})
    assert r.status_code == 400 and _connection()["credentials_enc"] is None


def test_a_refused_code_points_at_the_credentials(strava):
    client, _ = strava
    _save_app(client)
    r = _callback(client, _state(client), code="BAD")
    assert r.status_code == 502 and "Client ID" in r.text
    assert _connection()["credentials_enc"] is None


def test_reconnecting_the_same_athlete_keeps_the_import_progress(strava):
    client, _ = strava
    _connect(client)
    with db.cursor() as cur:
        cur.execute("UPDATE source_connections SET history_done = TRUE, history_page = 4, items_synced = 321, status = 'auth_failed' "
                    "WHERE user_id = %s AND source = 'strava'", (ALICE,))
    _callback(client, _state_after_login(client))
    row = _connection()
    assert row["status"] == "connected" and row["history_done"] is True and row["items_synced"] == 321


def _state_after_login(client):
    _login(client)
    return _state(client)


def test_a_different_strava_account_starts_over_and_drops_the_old_activities(strava):
    client, fake = strava
    _connect(client)
    with db.cursor() as cur:
        cur.execute("INSERT INTO activities (user_id, source, activity_id, start_time, name) VALUES (%s, 'strava', 1, now(), 'old athlete')", (ALICE,))
        cur.execute("INSERT INTO activities (user_id, source, activity_id, start_time, name) VALUES (%s, 'garmin', 1, now(), 'garmin run')", (ALICE,))
        cur.execute("UPDATE source_connections SET history_done = TRUE WHERE user_id = %s AND source = 'strava'", (ALICE,))
    fake.athlete = {"id": 999, "firstname": "Bob", "lastname": "Other"}
    _callback(client, _state_after_login(client))
    row = _connection()
    assert row["external_id"] == "999" and row["history_done"] is False
    with db.cursor() as cur:
        cur.execute("SELECT source, name FROM activities WHERE user_id = %s", (ALICE,))
        assert cur.fetchall() == [{"source": "garmin", "name": "garmin run"}]


# ─── changing the app, sync now, disconnect ───────────────────────────────────

def test_changing_the_app_needs_a_new_sign_in_but_keeps_the_data(strava):
    client, _ = strava
    _connect(client)
    with db.cursor() as cur:
        cur.execute("INSERT INTO activities (user_id, source, activity_id, start_time, name) VALUES (%s, 'strava', 1, now(), 'kept')", (ALICE,))
    assert _save_app(client, ALICE_APP).status_code == 200                       # same values: nothing happens
    assert _connection()["credentials_enc"] is not None

    assert _save_app(client, ("55555", "c" * 40)).status_code == 200
    row = _connection()
    assert row["status"] == "pending" and row["credentials_enc"] is None
    assert json.loads(decrypt_credential(row["app_credentials_enc"]))["client_id"] == "55555"
    with db.cursor() as cur:
        cur.execute("SELECT name FROM activities WHERE user_id = %s AND source = 'strava'", (ALICE,))
        assert cur.fetchall() == [{"name": "kept"}]
    assert client.get("/api/strava/status").json()["connected"] is False


def test_sync_now_queues_a_sync_only_when_connected(strava):
    client, _ = strava
    assert client.post("/api/strava/sync").status_code == 400
    _save_app(client)
    assert client.post("/api/strava/sync").status_code == 400                    # an app alone is not a connection
    _callback(client, _state(client))
    with db.cursor() as cur:
        cur.execute("UPDATE source_connections SET sync_requested_at = NULL WHERE user_id = %s AND source = 'strava'", (ALICE,))
    _login(client)
    assert client.post("/api/strava/sync").json()["queued"] is True


def test_disconnect_forgets_everything_and_deletes_only_strava_data(strava):
    client, fake = strava
    _connect(client)
    with db.cursor() as cur:
        cur.execute("INSERT INTO activities (user_id, source, activity_id, start_time, name) VALUES (%s, 'strava', 5, now(), 's')", (ALICE,))
        cur.execute("INSERT INTO activities (user_id, source, activity_id, start_time, name) VALUES (%s, 'garmin', 5, now(), 'g')", (ALICE,))
        cur.execute("INSERT INTO daily_summary (user_id, source, date, steps) VALUES (%s, 'garmin', CURRENT_DATE, 10)", (ALICE,))
        cur.execute("INSERT INTO tours (user_id, source, name) VALUES (%s, 'strava', 'strava tour') RETURNING id", (ALICE,))
        tour = cur.fetchone()["id"]
        cur.execute("INSERT INTO tour_activities (tour_id, activity_id) VALUES (%s, 5)", (tour,))

    assert client.delete("/api/strava").status_code == 204
    assert any(path.endswith("/deauthorize") and form["access_token"] == "acc" for path, form in fake.calls)   # Strava told
    assert _connection() is None                                                  # tokens AND the saved app are gone
    with db.cursor() as cur:
        cur.execute("SELECT source, name FROM activities WHERE user_id = %s", (ALICE,))
        assert cur.fetchall() == [{"source": "garmin", "name": "g"}]
        cur.execute("SELECT count(*) AS n FROM tours WHERE user_id = %s", (ALICE,))
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM tour_activities")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM daily_summary WHERE user_id = %s AND source = 'garmin'", (ALICE,))
        assert cur.fetchone()["n"] == 1                                           # Garmin's data is untouched
    st = client.get("/api/strava/status").json()
    assert st["connected"] is False and st["app_saved"] is False


def test_removing_an_app_that_was_never_connected(strava):
    client, fake = strava
    _save_app(client)
    assert client.delete("/api/strava").status_code == 204
    assert _connection() is None and fake.calls == []                              # nothing to tell Strava


def test_disconnect_works_even_when_strava_does_not_answer(strava, monkeypatch):
    client, fake = strava
    _connect(client)
    monkeypatch.setattr(strava_api, "OAUTH_BASE", "http://127.0.0.1:9")       # nothing listens there
    assert client.delete("/api/strava").status_code == 204
    assert _connection() is None
