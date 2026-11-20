"""
Connecting someone's Garmin Connect account from the app, two-factor included.

The API signs in to Garmin *while the person waits*, so a wrong email or password
is reported right away instead of silently failing later in the sync service.
When the account has two-factor authentication, Garmin answers "needs a code":
the half-finished sign-in is kept here in memory (one API process, see the
Dockerfile) until the person types the code Garmin sent them, then completed.

Either way the result is Garmin's OAuth tokens, written to the folder the sync
service reads (TOKEN_ROOT/<user id>, shared through docker-compose), so the sync
never needs a code itself. The email and password are stored by main.py only
after Garmin accepted them.
"""

import os
import re
import shutil
import threading
import time
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

TOKEN_ROOT = Path(os.getenv("GARTH_TOKEN_ROOT", "/root/.garth"))
CODE_TTL_SECONDS = 10 * 60   # how long a "type your code" step stays open
MAX_CODE_TRIES = 3           # wrong codes before starting over

# Sign-in attempts per person, to spare their Garmin account a lockout.
ATTEMPT_WINDOW_SECONDS = 15 * 60
MAX_ATTEMPTS = 6


class LinkError(Exception):
    """A connect step that failed in a way the app explains to the person."""

    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


_lock = threading.Lock()
_pending: dict[str, dict] = {}       # user id → half-finished two-factor sign-in
_attempts: dict[str, list[float]] = {}


def new_client(email: str, password: str) -> Garmin:
    """The Garmin client (a seam the tests replace with a fake)."""
    return Garmin(email=email, password=password, return_on_mfa=True)


def token_dir(user_id: str) -> Path:
    # User ids are quarc-auth UUIDs; refuse anything that could leave TOKEN_ROOT.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user_id):
        raise LinkError("bad_user", 400)
    return TOKEN_ROOT / user_id


def _count_attempt(user_id: str) -> None:
    now = time.time()
    with _lock:
        recent = [t for t in _attempts.get(user_id, []) if now - t < ATTEMPT_WINDOW_SECONDS]
        if len(recent) >= MAX_ATTEMPTS:
            _attempts[user_id] = recent
            raise LinkError("too_many_attempts", 429)
        recent.append(now)
        _attempts[user_id] = recent


def _save_tokens(user_id: str, client: Garmin) -> None:
    folder = token_dir(user_id)
    # Drop whatever was there: it may belong to a different Garmin account.
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    client.client.dump(str(folder))


def _sign_in(client: Garmin) -> str | None:
    try:
        status, _ = client.login()
        return status
    except GarminConnectAuthenticationError:
        raise LinkError("invalid_credentials", 400)
    except GarminConnectTooManyRequestsError:
        raise LinkError("rate_limited", 429)
    except Exception:
        raise LinkError("garmin_unreachable", 502)


def start(user_id: str, email: str, password: str) -> dict:
    """Sign in to Garmin. Returns {"status": "connected"} (tokens saved) or
    {"status": "needs_code", "method": "email" | "phone" | None}."""
    _count_attempt(user_id)
    client = new_client(email, password)
    status = _sign_in(client)
    if status == "needs_mfa":
        method = str(getattr(client.client, "_mfa_method", "") or "").lower()
        with _lock:
            _pending[user_id] = {
                "client": client, "email": email, "password": password,
                "started": time.time(), "tries": 0,
            }
        return {"status": "needs_code", "method": "email" if "email" in method else ("phone" if method else None)}
    _save_tokens(user_id, client)
    with _lock:
        _pending.pop(user_id, None)
    return {"status": "connected"}


def has_pending_code(user_id: str) -> bool:
    with _lock:
        p = _pending.get(user_id)
        if p and time.time() - p["started"] > CODE_TTL_SECONDS:
            _pending.pop(user_id, None)
            p = None
        return p is not None


def finish(user_id: str, code: str) -> tuple[str, str]:
    """Complete a two-factor sign-in with the code Garmin sent. Returns the
    (email, password) that Garmin has now accepted, for main.py to store."""
    code = re.sub(r"\s+", "", code or "")
    if not code:
        raise LinkError("invalid_code", 400)
    if not has_pending_code(user_id):
        raise LinkError("code_expired", 410)
    with _lock:
        p = _pending[user_id]
    try:
        p["client"].resume_login(None, code)
    except GarminConnectTooManyRequestsError:
        raise LinkError("rate_limited", 429)
    except GarminConnectAuthenticationError:
        with _lock:
            p["tries"] += 1
            if p["tries"] >= MAX_CODE_TRIES:
                _pending.pop(user_id, None)
                raise LinkError("code_expired", 410)
        raise LinkError("invalid_code", 400)
    except Exception:
        raise LinkError("garmin_unreachable", 502)
    _save_tokens(user_id, p["client"])
    with _lock:
        _pending.pop(user_id, None)
    return p["email"], p["password"]


def forget(user_id: str) -> None:
    """Disconnect: drop any open code step and Garmin's tokens."""
    with _lock:
        _pending.pop(user_id, None)
    shutil.rmtree(token_dir(user_id), ignore_errors=True)


def cancel(user_id: str) -> None:
    with _lock:
        _pending.pop(user_id, None)
