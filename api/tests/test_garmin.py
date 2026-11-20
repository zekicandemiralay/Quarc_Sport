"""Connecting Garmin from the app: checked with Garmin first, two-factor code in
the app, import queued right away, status for the app. Garmin itself is faked."""

import json
from pathlib import Path

import pytest
from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

import db
import garmin_link
from conftest import make_token

ALICE = "11111111-1111-1111-1111-111111111111"

# email → (password, has two-factor)
ACCOUNTS = {"me@example.com": ("hunter2", False), "mfa@example.com": ("pw", True)}
CODE = "123456"


class FakeClient:
    _mfa_method = None
    token = None

    def dump(self, path):
        Path(path, "garmin_tokens.json").write_text(json.dumps({"di_token": self.token}))


class FakeGarmin:
    rate_limited = False

    def __init__(self, email, password):
        self.email, self.password = email, password
        self.client = FakeClient()

    def login(self, tokenstore=None):
        if FakeGarmin.rate_limited:
            raise GarminConnectTooManyRequestsError("429")
        account = ACCOUNTS.get(self.email)
        if not account or account[0] != self.password:
            raise GarminConnectAuthenticationError("401 Unauthorized")
        if account[1]:
            self.client._mfa_method = "email"
            return "needs_mfa", None
        self.client.token = f"token-for-{self.email}"
        return None, None

    def resume_login(self, state, code):
        if code != CODE:
            raise GarminConnectAuthenticationError("MFA verification failed")
        self.client.token = f"token-for-{self.email}"
        return None, None


@pytest.fixture()
def garmin(alice, tmp_path, monkeypatch):
    monkeypatch.setattr(garmin_link, "new_client", FakeGarmin)
    monkeypatch.setattr(garmin_link, "TOKEN_ROOT", tmp_path)
    garmin_link._pending.clear()
    garmin_link._attempts.clear()
    FakeGarmin.rate_limited = False
    return tmp_path


def tokens(root):
    p = root / ALICE / "garmin_tokens.json"
    return json.loads(p.read_text())["di_token"] if p.exists() else None


def queued():
    with db.cursor() as cur:
        cur.execute("SELECT sync_requested_at IS NOT NULL AS q FROM users WHERE id = %s", (ALICE,))
        return cur.fetchone()["q"]


def test_wrong_login_is_reported_and_nothing_is_saved(alice, garmin):
    r = alice.post("/api/garmin/connect", json={"email": "me@example.com", "password": "wrong"})
    assert r.status_code == 400 and r.json()["detail"] == "invalid_credentials"
    r = alice.post("/api/garmin/connect", json={"email": "nobody@example.com", "password": "x"})
    assert r.json()["detail"] == "invalid_credentials"          # no such Garmin account
    assert alice.get("/api/me").json()["has_garmin"] is False
    assert alice.get("/api/garmin/status").json()["status"] == "not_connected"
    assert tokens(garmin) is None


def test_connect_saves_and_starts_the_import_right_away(alice, garmin):
    r = alice.post("/api/garmin/connect", json={"email": " me@example.com ", "password": "hunter2"})
    assert r.status_code == 200 and r.json() == {"status": "connected"}
    me = alice.get("/api/me").json()
    assert me["has_garmin"] is True and me["garmin_status"] == "connected"
    assert tokens(garmin) == "token-for-me@example.com"
    assert queued()
    s = alice.get("/api/garmin/status").json()
    assert s["status"] == "connected" and s["email"] == "me•••@example.com"
    assert s["queued"] is True and s["first_import"] is True and s["pending_code"] is False
    assert "hunter2" not in json.dumps(s)


def test_two_factor_code_is_entered_in_the_app(alice, garmin):
    r = alice.post("/api/garmin/connect", json={"email": "mfa@example.com", "password": "pw"})
    assert r.json() == {"status": "needs_code", "method": "email"}
    assert alice.get("/api/me").json()["has_garmin"] is False     # not saved before the code
    assert alice.get("/api/garmin/status").json()["pending_code"] is True

    r = alice.post("/api/garmin/code", json={"code": "000000"})
    assert r.status_code == 400 and r.json()["detail"] == "invalid_code"
    r = alice.post("/api/garmin/code", json={"code": " 123 456 "})   # spaces are fine
    assert r.status_code == 200 and r.json() == {"status": "connected"}

    assert alice.get("/api/me").json()["has_garmin"] is True
    assert tokens(garmin) == "token-for-mfa@example.com"
    assert queued()
    assert alice.get("/api/garmin/status").json()["pending_code"] is False


def test_code_step_closes_after_three_wrong_codes(alice, garmin):
    alice.post("/api/garmin/connect", json={"email": "mfa@example.com", "password": "pw"})
    assert alice.post("/api/garmin/code", json={"code": "1"}).json()["detail"] == "invalid_code"
    assert alice.post("/api/garmin/code", json={"code": "2"}).json()["detail"] == "invalid_code"
    r = alice.post("/api/garmin/code", json={"code": "3"})
    assert r.status_code == 410 and r.json()["detail"] == "code_expired"
    assert alice.post("/api/garmin/code", json={"code": CODE}).status_code == 410


def test_code_without_a_sign_in_and_cancel(alice, garmin):
    assert alice.post("/api/garmin/code", json={"code": CODE}).json()["detail"] == "code_expired"
    alice.post("/api/garmin/connect", json={"email": "mfa@example.com", "password": "pw"})
    assert alice.post("/api/garmin/cancel").status_code == 204
    assert alice.get("/api/garmin/status").json()["pending_code"] is False


def test_code_steps_are_per_person(client, garmin):
    client.cookies.set("token", make_token())
    client.post("/api/garmin/connect", json={"email": "mfa@example.com", "password": "pw"})
    client.cookies.set("token", make_token(user_id="22222222-2222-2222-2222-222222222222", username="bob"))
    assert client.post("/api/garmin/code", json={"code": CODE}).json()["detail"] == "code_expired"


def test_attempts_are_limited_to_spare_the_garmin_account(alice, garmin):
    for _ in range(garmin_link.MAX_ATTEMPTS):
        alice.post("/api/garmin/connect", json={"email": "me@example.com", "password": "wrong"})
    r = alice.post("/api/garmin/connect", json={"email": "me@example.com", "password": "hunter2"})
    assert r.status_code == 429 and r.json()["detail"] == "too_many_attempts"


def test_garmin_rate_limit_is_reported(alice, garmin):
    FakeGarmin.rate_limited = True
    r = alice.post("/api/garmin/connect", json={"email": "me@example.com", "password": "hunter2"})
    assert r.status_code == 429 and r.json()["detail"] == "rate_limited"


def test_reconnect_asks_for_a_new_code_without_the_password(alice, garmin):
    alice.post("/api/garmin/connect", json={"email": "mfa@example.com", "password": "pw"})
    alice.post("/api/garmin/code", json={"code": CODE})
    with db.cursor() as cur:   # later the sync service found Garmin wants a code again
        cur.execute("UPDATE users SET garmin_status = 'needs_code', sync_requested_at = NULL WHERE id = %s", (ALICE,))
    assert alice.get("/api/me").json()["garmin_status"] == "needs_code"

    assert alice.post("/api/garmin/reconnect").json() == {"status": "needs_code", "method": "email"}
    assert alice.post("/api/garmin/code", json={"code": CODE}).json() == {"status": "connected"}
    assert alice.get("/api/garmin/status").json()["status"] == "connected"
    assert queued()


def test_sync_now(alice, garmin):
    assert alice.post("/api/garmin/sync").status_code == 400        # not connected
    alice.post("/api/garmin/connect", json={"email": "me@example.com", "password": "hunter2"})
    with db.cursor() as cur:
        cur.execute("UPDATE users SET sync_requested_at = NULL WHERE id = %s", (ALICE,))
    r = alice.post("/api/garmin/sync")
    assert r.status_code == 200 and r.json()["queued"] is True


def test_disconnect_forgets_login_tokens_and_status(alice, garmin):
    alice.post("/api/garmin/connect", json={"email": "me@example.com", "password": "hunter2"})
    assert alice.delete("/api/garmin/credentials").status_code == 204
    assert tokens(garmin) is None
    me = alice.get("/api/me").json()
    assert me["has_garmin"] is False and me["garmin_status"] is None
    assert alice.get("/api/garmin/status").json()["status"] == "not_connected"


def test_import_progress(alice, garmin):
    alice.post("/api/garmin/connect", json={"email": "me@example.com", "password": "hunter2"})
    with db.cursor() as cur:
        cur.execute(
            "UPDATE users SET synced_until = '2026-11-20', history_from = '2025-11-20', "
            "history_target = '2016-11-20', last_sync_at = now() WHERE id = %s", (ALICE,))
    s = alice.get("/api/garmin/status").json()
    assert s["first_import"] is False and s["history_done"] is False and s["history_percent"] == 10
    with db.cursor() as cur:
        cur.execute("UPDATE users SET history_from = history_target WHERE id = %s", (ALICE,))
    s = alice.get("/api/garmin/status").json()
    assert s["history_done"] is True and s["history_percent"] == 100


def test_old_apps_can_still_save_a_login(alice, garmin):
    # app versions before 1.0.1 PUT the login unchecked; the sync service checks it
    assert alice.put("/api/garmin/credentials", json={"email": "x@example.com", "password": "y"}).status_code == 200
    assert alice.get("/api/me").json()["garmin_status"] == "connected"
    assert queued()
