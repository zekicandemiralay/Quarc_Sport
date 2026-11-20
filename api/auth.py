"""
Authentication for Quarc Sport.

Accounts live in quarc-auth (Quarc_Store/auth) — the one login shared by every
Quarc app. It sets an httpOnly `token` cookie (a JWT signed with JWT_SECRET) and
every app verifies that cookie locally, with no network call per request. This
module is the Python twin of the `requireAuth` middleware in the Node apps
(e.g. Quarc_Calendar/backend/src/middleware/auth.js), including its sliding
session renewal.

Separately, CREDENTIALS_KEY (a Fernet key) encrypts each person's Garmin Connect
email + password at rest. It has nothing to do with login.
"""

import os
import time

import jwt
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, Request, Response

import db

JWT_SECRET = os.getenv("JWT_SECRET", "")
SECURE_COOKIE = os.getenv("SECURE_COOKIE", "true").lower() == "true"

# Chrome/Edge/Chromium WebViews cap a cookie's Max-Age at 400 days and silently
# truncate anything longer.
MAX_COOKIE_DAYS = 400


# ─── Garmin credential encryption ─────────────────────────────────────────────

_fernet: Fernet | None = None


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.getenv("CREDENTIALS_KEY", "")
        if not key:
            raise RuntimeError("CREDENTIALS_KEY env var is not set")
        _fernet = Fernet(key.encode())
    return _fernet


def encrypt_credential(plaintext: str) -> bytes:
    return get_fernet().encrypt(plaintext.encode())


def decrypt_credential(ciphertext: bytes) -> str:
    return get_fernet().decrypt(bytes(ciphertext)).decode()


# ─── Login cookie ─────────────────────────────────────────────────────────────

def _renew_if_stale(response: Response, claims: dict) -> None:
    """Sliding session — same rule as the Node apps' copy.

    Every request that reaches here already carries a token quarc-auth judged
    valid. Past the halfway point of its life, re-issue it with a full new window
    so a session that is used keeps working. The renewal length is derived from
    the token itself (exp - iat), so SESSION_LIFETIME_DAYS only has to be set in
    quarc-auth's .env.
    """
    iat, exp = claims.get("iat"), claims.get("exp")
    if not iat or not exp:
        return
    lifetime = exp - iat
    if lifetime <= 0 or time.time() - iat < lifetime / 2:
        return

    fresh = min(lifetime, MAX_COOKIE_DAYS * 86400)
    renewed = {k: v for k, v in claims.items() if k not in ("iat", "exp")}
    now = int(time.time())
    renewed["iat"], renewed["exp"] = now, now + fresh
    response.set_cookie(
        "token",
        jwt.encode(renewed, JWT_SECRET, algorithm="HS256"),
        max_age=fresh,
        httponly=True,
        secure=SECURE_COOKIE,
        samesite="none" if SECURE_COOKIE else "lax",
    )


def get_current_user(request: Request, response: Response) -> dict:
    """FastAPI dependency: the signed-in quarc-auth user, or 401."""
    token = request.cookies.get("token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not JWT_SECRET:
        raise HTTPException(status_code=500, detail="JWT_SECRET is not configured")
    try:
        claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Session expired")
    if not claims.get("id"):
        raise HTTPException(status_code=401, detail="Session expired")
    _renew_if_stale(response, claims)
    return {
        "id": str(claims["id"]),
        "username": claims.get("username") or "",
        "is_admin": claims.get("role") == "admin",
    }


def require_user(user: dict = Depends(get_current_user)) -> dict:
    """Like get_current_user, but also makes sure Quarc Sport has a row for them.

    Everything that reads or writes someone's data goes through this, so the
    foreign keys on daily_summary/activities/... always have a user to point at.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (id, username) VALUES (%s, %s)
            ON CONFLICT (id) DO UPDATE SET username = EXCLUDED.username
            WHERE users.username IS DISTINCT FROM EXCLUDED.username
            """,
            (user["id"], user["username"]),
        )
    return user
