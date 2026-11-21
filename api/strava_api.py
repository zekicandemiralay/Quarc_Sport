"""
Strava connection: everyone brings their OWN Strava API application.

Strava's API is official and free of passwords, but every app needs a registration at
strava.com/settings/api (Strava currently asks for a Strava subscription to create one). A new
Strava app only lets its owner connect — which is exactly what a personal app needs, so nobody
depends on the server owner's app or on Strava's 10-person limit for shared apps.

Per person: they register an app, paste its Client ID and Client Secret into Settings (stored
encrypted, like Garmin's login), then sign in on Strava's own page and allow access. Their
activities are stored with source = 'strava' by the sync service (sync/strava.py) and are never
mixed with Garmin or the phone.
"""

import json
import os
import re
import time
from urllib.parse import urlencode, urlparse

import jwt
import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import auth
import db
from auth import require_user, encrypt_credential, decrypt_credential

router = APIRouter(prefix="/api/strava")

OAUTH_BASE = os.getenv("STRAVA_OAUTH_BASE", "https://www.strava.com/oauth").rstrip("/")
SCOPE = "read,activity:read_all"
STATE_MINUTES = 10
HTTP_TIMEOUT = 30


def _public_url() -> str:
    return os.getenv("PUBLIC_URL", "").rstrip("/")


def _redirect_uri() -> str:
    return f"{_public_url()}/api/strava/callback"


def _callback_domain() -> str:
    """What the person types into Strava's "Authorization Callback Domain": the host name only."""
    return urlparse(_public_url()).hostname or ""


def _app_credentials(user_id: str) -> dict | None:
    """{client_id, client_secret} of this person's own Strava app, if they saved one."""
    with db.cursor() as cur:
        cur.execute("SELECT app_credentials_enc FROM source_connections WHERE user_id = %s AND source = 'strava'", (user_id,))
        row = cur.fetchone()
    if not row or row["app_credentials_enc"] is None:
        return None
    try:
        return json.loads(decrypt_credential(row["app_credentials_enc"]))
    except Exception:
        return None


def _make_state(user_id: str) -> str:
    # Signed, short-lived: the callback arrives from Strava's redirect, possibly in another browser
    # (the Android app opens Strava outside the app), so it can't rely on the login cookie.
    return jwt.encode(
        {"sub": user_id, "purpose": "strava_oauth", "exp": int(time.time()) + STATE_MINUTES * 60},
        auth.JWT_SECRET, algorithm="HS256",
    )


def _read_state(token: str | None) -> str | None:
    try:
        claims = jwt.decode(token or "", auth.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    return claims.get("sub") if claims.get("purpose") == "strava_oauth" else None


def _iso(v):
    return v.isoformat() if v else None


# ─── Status ───────────────────────────────────────────────────────────────────

@router.get("/status")
def status(user: dict = Depends(require_user)):
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT status, error, display_name, last_sync_at, syncing_since, sync_requested_at,
                   retry_after, items_synced, history_done, credentials_enc IS NOT NULL AS has_tokens,
                   app_credentials_enc IS NOT NULL AS has_app
            FROM source_connections WHERE user_id = %s AND source = 'strava'
            """,
            (user["id"],),
        )
        row = cur.fetchone()
    connected = bool(row and row["has_tokens"])
    app = _app_credentials(user["id"]) if row and row["has_app"] else None
    return {
        "configured": bool(_public_url()),                  # the server knows its own address (needed for Strava's redirect)
        "website": _public_url(),                           # what the person enters into Strava's form
        "callback_domain": _callback_domain(),
        "app_saved": app is not None,
        "client_id": app["client_id"] if app else None,     # the secret never leaves the server
        "connected": connected,
        "status": (row["status"] if connected else "pending") if row else "not_connected",
        "error": row["error"] if connected else None,
        "athlete": row["display_name"] if connected else None,
        "last_sync_at": _iso(row["last_sync_at"]) if connected else None,
        "syncing": bool(connected and row["syncing_since"]),
        "queued": bool(connected and row["sync_requested_at"]),
        "retry_after": _iso(row["retry_after"]) if connected else None,
        "imported": row["items_synced"] if connected else 0,
        "history_done": bool(connected and row["history_done"]),
    }


# ─── The person's own Strava app ──────────────────────────────────────────────

class AppBody(BaseModel):
    client_id: str
    client_secret: str


@router.put("/app")
def save_app(body: AppBody, user: dict = Depends(require_user)):
    """Save the Client ID and Client Secret of the person's own Strava app (encrypted).
    Changing them means signing in to Strava again; what was already imported stays."""
    client_id, secret = body.client_id.strip(), body.client_secret.strip()
    if not re.fullmatch(r"\d{3,10}", client_id):
        raise HTTPException(status_code=400, detail="invalid_client_id")
    if not re.fullmatch(r"[A-Za-z0-9]{20,64}", secret):
        raise HTTPException(status_code=400, detail="invalid_client_secret")
    existing = _app_credentials(user["id"])
    enc = encrypt_credential(json.dumps({"client_id": client_id, "client_secret": secret}))
    with db.cursor() as cur:
        if existing == {"client_id": client_id, "client_secret": secret}:
            return {"ok": True}                      # nothing changed: keep the connection as it is
        cur.execute(
            """
            INSERT INTO source_connections (user_id, source, status, app_credentials_enc)
            VALUES (%s, 'strava', 'pending', %s)
            ON CONFLICT (user_id, source) DO UPDATE SET
                app_credentials_enc = EXCLUDED.app_credentials_enc,
                credentials_enc = NULL, status = 'pending', error = NULL,
                sync_requested_at = NULL, retry_after = NULL
            """,
            (user["id"], enc),
        )
    return {"ok": True}


# ─── Connect ──────────────────────────────────────────────────────────────────

@router.post("/authorize")
def authorize(user: dict = Depends(require_user)):
    """The address to send the person to, to let their own Strava app read their activities."""
    app = _app_credentials(user["id"])
    if not app:
        raise HTTPException(status_code=400, detail="no_app")
    if not _public_url():
        raise HTTPException(status_code=400, detail="no_public_url")
    query = urlencode({
        "client_id": app["client_id"],
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": SCOPE,
        "state": _make_state(user["id"]),
    })
    return {"url": f"{OAUTH_BASE}/authorize?{query}"}


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Quarc Sport</title>
<style>body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#020617;color:#e2e8f0;
font-family:system-ui,sans-serif}}main{{max-width:26rem;margin:1rem;padding:2rem;background:#0f172a;border:1px solid #1e293b;
border-radius:1rem;text-align:center}}h1{{font-size:1.1rem;margin:0 0 .75rem;color:{color}}}p{{margin:.4rem 0;font-size:.9rem;color:#94a3b8}}
a{{display:inline-block;margin-top:1rem;color:#60a5fa}}</style></head><body><main>
<h1>{title_en}<br><small>{title_tr}</small></h1><p>{body_en}</p><p>{body_tr}</p>{link}</main></body></html>"""


def _page(ok: bool, title_en: str, title_tr: str, body_en: str, body_tr: str, status_code: int = 200) -> HTMLResponse:
    back = _public_url()
    link = f'<a href="{back}/">Quarc Sport</a>' if back else ""
    return HTMLResponse(
        _PAGE.format(color="#4ade80" if ok else "#f87171", title_en=title_en, title_tr=title_tr,
                     body_en=body_en, body_tr=body_tr, link=link),
        status_code=status_code,
    )


def _delete_strava_data(cur, user_id: str) -> None:
    for table in ("tours", "activities", "daily_summary", "sleep", "hrv"):   # tours first: their links cascade
        cur.execute(f"DELETE FROM {table} WHERE user_id = %s AND source = 'strava'", (user_id,))


@router.get("/callback", response_class=HTMLResponse)
def callback(code: str | None = Query(default=None), state: str | None = Query(default=None),
             error: str | None = Query(default=None), scope: str | None = Query(default=None)):
    """Strava sends the person's browser here after they said yes (or no)."""
    if error:
        return _page(False, "Strava was not connected", "Strava bağlanmadı",
                     "Access was not granted. You can try again from Settings.",
                     "Erişim verilmedi. Ayarlar'dan tekrar deneyebilirsin.", 400)
    user_id = _read_state(state)
    app = _app_credentials(user_id) if user_id else None
    if not user_id or not code or not app:
        return _page(False, "This link has expired", "Bu bağlantının süresi doldu",
                     "Start again from Settings → Strava.", "Ayarlar → Strava'dan yeniden başla.", 400)
    if "activity:read" not in (scope or "").replace("activity:read_all", "activity:read").split(","):
        return _page(False, "Activity access is needed", "Aktivite erişimi gerekli",
                     "Tick “View data about your activities” when Strava asks, then try again.",
                     "Strava sorduğunda “Aktiviteleriniz hakkındaki verileri görüntüleyin” kutusunu işaretle ve tekrar dene.", 400)
    try:
        r = requests.post(f"{OAUTH_BASE}/token", data={
            "client_id": app["client_id"], "client_secret": app["client_secret"], "code": code, "grant_type": "authorization_code",
        }, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        token = r.json()
        creds = {"access_token": token["access_token"], "refresh_token": token["refresh_token"], "expires_at": token["expires_at"]}
        athlete = token.get("athlete") or {}
    except Exception:
        return _page(False, "Strava could not be connected", "Strava bağlanamadı",
                     "Strava did not accept the request. Check the Client ID and Client Secret you saved, then try again.",
                     "Strava isteği kabul etmedi. Kaydettiğin Client ID ve Client Secret'i kontrol edip tekrar dene.", 502)

    athlete_id = str(athlete.get("id") or "")
    name = " ".join(x for x in (athlete.get("firstname"), athlete.get("lastname")) if x).strip() or None
    with db.cursor() as cur:
        cur.execute("SELECT external_id FROM source_connections WHERE user_id = %s AND source = 'strava'", (user_id,))
        previous = cur.fetchone()
        different = bool(previous and previous["external_id"] and athlete_id and previous["external_id"] != athlete_id)
        if different:
            _delete_strava_data(cur, user_id)      # a different Strava account: the old activities are not theirs any more
        cur.execute(
            """
            UPDATE source_connections SET
                status = 'connected', error = NULL, retry_after = NULL, sync_requested_at = now(),
                credentials_enc = %s, external_id = %s, display_name = %s
            WHERE user_id = %s AND source = 'strava'
            """,
            (encrypt_credential(json.dumps(creds)), athlete_id or None, name, user_id),
        )
        if previous is None or not previous["external_id"] or different:
            # first connection (or another account): import the whole history from the beginning
            cur.execute(
                "UPDATE source_connections SET history_done = FALSE, history_page = 1, history_before = NULL, items_synced = 0, last_sync_at = NULL "
                "WHERE user_id = %s AND source = 'strava'", (user_id,))
    return _page(True, "Strava is connected", "Strava bağlandı",
                 "Your activities are importing now. You can close this tab and go back to Quarc Sport.",
                 "Aktivitelerin şimdi içe aktarılıyor. Bu sekmeyi kapatıp Quarc Sport'a dönebilirsin.")


# ─── Sync now / disconnect ────────────────────────────────────────────────────

@router.post("/sync")
def sync_now(user: dict = Depends(require_user)):
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE source_connections SET sync_requested_at = COALESCE(sync_requested_at, now()), retry_after = NULL
            WHERE user_id = %s AND source = 'strava' AND credentials_enc IS NOT NULL AND status IN ('connected', 'error')
            RETURNING user_id
            """,
            (user["id"],),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=400, detail="not_connected")
    return status(user)


@router.delete("", status_code=204)
def disconnect(user: dict = Depends(require_user)):
    """Remove Strava completely: tell Strava to drop the access, forget the tokens AND the saved app
    credentials, and delete the Strava activities stored here (Strava's API terms ask for that)."""
    with db.cursor() as cur:
        cur.execute("SELECT credentials_enc FROM source_connections WHERE user_id = %s AND source = 'strava'", (user["id"],))
        row = cur.fetchone()
    if row and row["credentials_enc"]:
        try:   # best effort: the person is disconnected here whether or not Strava answers
            creds = json.loads(decrypt_credential(row["credentials_enc"]))
            requests.post(f"{OAUTH_BASE}/deauthorize", data={"access_token": creds["access_token"]}, timeout=10)
        except Exception:
            pass
    with db.cursor() as cur:
        _delete_strava_data(cur, user["id"])
        cur.execute("DELETE FROM source_connections WHERE user_id = %s AND source = 'strava'", (user["id"],))
