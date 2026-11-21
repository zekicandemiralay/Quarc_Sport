"""
Quarc Sport — FastAPI backend

Login is not handled here: quarc-auth (Quarc_Store/auth) is the one login shared by
every Quarc app, and nginx sends /api/auth/* straight to it. This API only checks
the cookie it sets (see auth.py). Everything here lives under /api/.
"""

import logging
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Optional, List

import psycopg2
from fastapi import FastAPI, Query, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

import db
import garmin_link
import health_connect_api
import strava_api
from auth import require_user, encrypt_credential, decrypt_credential

log = logging.getLogger("quarc-sport")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # The database container can still be starting when this one comes up.
    for attempt in range(30):
        try:
            db.ensure_schema()
            break
        except psycopg2.OperationalError as e:
            log.warning("database not ready (%s) — retrying", str(e).strip().splitlines()[0])
            time.sleep(2)
    else:
        raise RuntimeError("database did not become ready")
    yield


app = FastAPI(title="Quarc Sport API", version="3.0.0", lifespan=lifespan)

# Like the other Quarc backends: reflect the caller's origin and allow cookies
# (the Android app's WebView calls the server from a different origin).
app.include_router(strava_api.router)
app.include_router(health_connect_api.router)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def default_range() -> tuple[date, date]:
    end = date.today()
    start = end - timedelta(days=30)
    return start, end


# ─── Health (public) ──────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    try:
        with db.cursor() as cur:
            cur.execute("SELECT 1")
        return {"ok": True, "service": "quarc-sport-api"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))


# ─── Me + Garmin connection ───────────────────────────────────────────────────

class GarminCredentialsBody(BaseModel):
    email: str
    password: str


class GarminCodeBody(BaseModel):
    code: str


@app.get("/api/me")
def get_me(user: dict = Depends(require_user)):
    with db.cursor() as cur:
        cur.execute(
            "SELECT garmin_email_enc IS NOT NULL AS has_garmin, garmin_status FROM users WHERE id = %s",
            (user["id"],),
        )
        row = cur.fetchone()
    has_garmin = bool(row and row["has_garmin"])
    return {
        "id": user["id"],
        "username": user["username"],
        "has_garmin": has_garmin,
        "garmin_status": (row["garmin_status"] or "connected") if has_garmin else None,
    }


def _store_garmin_login(user_id: str, email: str, password: str) -> None:
    """Save a Garmin login (encrypted) and ask the sync service to start right away."""
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE users SET garmin_email_enc = %s, garmin_pass_enc = %s,
                   garmin_status = 'connected', garmin_error = NULL, retry_after = NULL,
                   sync_requested_at = now()
            WHERE id = %s
            """,
            (encrypt_credential(email), encrypt_credential(password), user_id),
        )


def _mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:2]}•••@{domain}" if domain else f"{email[:2]}•••"


def _garmin_status(user_id: str) -> dict:
    """What the app shows about the Garmin connection and the import."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT garmin_email_enc, garmin_status, garmin_error, sync_requested_at, syncing_since,
                   last_sync_at, retry_after, synced_until, history_from, history_target
            FROM users WHERE id = %s
            """,
            (user_id,),
        )
        row = cur.fetchone() or {}
    connected = row.get("garmin_email_enc") is not None
    email = None
    if connected:
        try:
            email = _mask_email(decrypt_credential(row["garmin_email_enc"]))
        except Exception:
            email = None
    until, start, target = row.get("synced_until"), row.get("history_from"), row.get("history_target")
    history_done = bool(start and target and start <= target)
    percent = None
    if until and start and target:
        span = max((until - target).days, 1)
        percent = 100 if history_done else max(0, min(99, round(100 * (until - start).days / span)))
    iso = lambda v: v.isoformat() if v else None  # noqa: E731
    return {
        "status": (row.get("garmin_status") or "connected") if connected else "not_connected",
        "email": email,
        "pending_code": garmin_link.has_pending_code(user_id),
        "queued": connected and row.get("sync_requested_at") is not None,
        "syncing": row.get("syncing_since") is not None,
        "first_import": connected and until is None,
        "last_sync_at": iso(row.get("last_sync_at")),
        "retry_after": iso(row.get("retry_after")),
        "synced_until": iso(until),
        "history_from": iso(start),
        "history_target": iso(target),
        "history_done": history_done,
        "history_percent": percent,
    }


def _link_error(e: garmin_link.LinkError) -> HTTPException:
    # detail is a code the app translates (settings.garmin.errors.<code>)
    return HTTPException(status_code=e.status, detail=e.code)


@app.get("/api/garmin/status")
def get_garmin_status(user: dict = Depends(require_user)):
    return _garmin_status(user["id"])


@app.post("/api/garmin/connect")
def connect_garmin(body: GarminCredentialsBody, user: dict = Depends(require_user)):
    """Check the login with Garmin, then save it. Two-factor accounts get
    {"status": "needs_code"} and finish with POST /api/garmin/code."""
    email = body.email.strip()
    if not email or not body.password:
        raise HTTPException(status_code=400, detail="missing_fields")
    try:
        result = garmin_link.start(user["id"], email, body.password)
    except garmin_link.LinkError as e:
        raise _link_error(e)
    if result["status"] == "connected":
        _store_garmin_login(user["id"], email, body.password)
    return result


@app.post("/api/garmin/code")
def submit_garmin_code(body: GarminCodeBody, user: dict = Depends(require_user)):
    try:
        email, password = garmin_link.finish(user["id"], body.code)
    except garmin_link.LinkError as e:
        raise _link_error(e)
    _store_garmin_login(user["id"], email, password)
    return {"status": "connected"}


@app.post("/api/garmin/reconnect")
def reconnect_garmin(user: dict = Depends(require_user)):
    """Sign in again with the saved login, e.g. when Garmin asks for a new
    two-factor code. The person doesn't have to type their password again."""
    with db.cursor() as cur:
        cur.execute("SELECT garmin_email_enc, garmin_pass_enc FROM users WHERE id = %s", (user["id"],))
        row = cur.fetchone()
    if not row or row["garmin_email_enc"] is None:
        raise HTTPException(status_code=400, detail="not_connected")
    email, password = decrypt_credential(row["garmin_email_enc"]), decrypt_credential(row["garmin_pass_enc"])
    try:
        result = garmin_link.start(user["id"], email, password)
    except garmin_link.LinkError as e:
        if e.code == "invalid_credentials":
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE users SET garmin_status = 'auth_failed', garmin_error = NULL WHERE id = %s",
                    (user["id"],),
                )
        raise _link_error(e)
    if result["status"] == "connected":
        _store_garmin_login(user["id"], email, password)
    return result


@app.post("/api/garmin/cancel", status_code=204)
def cancel_garmin_code(user: dict = Depends(require_user)):
    garmin_link.cancel(user["id"])


@app.post("/api/garmin/sync")
def sync_garmin_now(user: dict = Depends(require_user)):
    """Sync now: the sync service picks this person up within seconds."""
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE users SET sync_requested_at = COALESCE(sync_requested_at, now()), retry_after = NULL
            WHERE id = %s AND garmin_email_enc IS NOT NULL AND COALESCE(garmin_status, 'connected') IN ('connected', 'error')
            RETURNING id
            """,
            (user["id"],),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=400, detail="not_connected")
        cur.execute("UPDATE users SET garmin_status = 'connected', garmin_error = NULL WHERE id = %s", (user["id"],))
    return _garmin_status(user["id"])


@app.put("/api/garmin/credentials")
def set_garmin_credentials(body: GarminCredentialsBody, user: dict = Depends(require_user)):
    """Save a login without checking it first — what app versions before 1.0.1
    send. The sync service checks it and reports through the status."""
    garmin_link.forget(user["id"])
    _store_garmin_login(user["id"], body.email.strip(), body.password)
    return {"ok": True}


@app.delete("/api/garmin/credentials", status_code=204)
def delete_garmin_credentials(user: dict = Depends(require_user)):
    """Disconnect Garmin: the login and Garmin's tokens are deleted and syncing
    stops. Already-synced data stays."""
    garmin_link.forget(user["id"])
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE users SET garmin_email_enc = NULL, garmin_pass_enc = NULL, garmin_status = NULL,
                   garmin_error = NULL, sync_requested_at = NULL, retry_after = NULL
            WHERE id = %s
            """,
            (user["id"],),
        )



# ─── Sources ──────────────────────────────────────────────────────────────────
# Data comes from one source at a time: 'garmin', 'strava' or 'health_connect'. The app
# asks for ?source=<name> on every data route; sources are never merged or mixed.

SOURCES = ("garmin", "strava", "health_connect")

# What each table can hold; /api/sources says which of these a source really has, so the
# app only offers charts that have data behind them. Nothing is ever computed to fill gaps.
CAPABILITY_COLUMNS = {
    "daily_summary": ["steps", "distance_meters", "active_calories", "total_calories", "floors_ascended",
                      "active_time_seconds", "sedentary_seconds", "stress_avg", "body_battery_high",
                      "spo2_avg", "resting_hr", "min_hr_day", "max_hr_day"],
    "sleep": ["duration_seconds", "light_seconds", "deep_seconds", "rem_seconds", "awake_seconds",
              "sleep_score", "avg_spo2", "avg_respiration", "start_time"],
    "hrv": ["hrv_last_night", "hrv_weekly_avg"],
    "activities": ["distance_meters", "avg_hr", "max_hr", "calories", "avg_pace_sec_per_km", "aerobic_te",
                   "elevation_gain_m", "avg_speed_mps", "avg_cadence", "avg_power", "start_lat", "polyline", "country"],
}


def _capabilities(user_id: str) -> dict:
    """{source: {table: {column: True}}} — only columns holding at least one value."""
    caps: dict = {}
    with db.cursor() as cur:
        for table, columns in CAPABILITY_COLUMNS.items():
            flags = ", ".join(f"bool_or({c} IS NOT NULL) AS {c}" for c in columns)
            cur.execute(f"SELECT source, count(*) AS n, {flags} FROM {table} WHERE user_id = %s GROUP BY source", (user_id,))
            for row in cur.fetchall():
                entry = caps.setdefault(row["source"], {}).setdefault(table, {"_rows": row["n"]})
                for c in columns:
                    if row[c]:
                        entry[c] = True
    return caps


def _connections(user_id: str) -> dict:
    """{source: {status, last_sync_at, ...}} for everything connected."""
    out: dict = {}
    with db.cursor() as cur:
        cur.execute("SELECT garmin_email_enc IS NOT NULL AS connected, garmin_status, last_sync_at FROM users WHERE id = %s", (user_id,))
        g = cur.fetchone()
        if g and g["connected"]:
            out["garmin"] = {"status": g["garmin_status"] or "connected", "last_sync_at": g["last_sync_at"]}
        cur.execute("SELECT source, status, last_sync_at, display_name FROM source_connections "
                    "WHERE user_id = %s AND (source <> 'strava' OR credentials_enc IS NOT NULL)", (user_id,))
        for r in cur.fetchall():
            out[r["source"]] = {"status": r["status"], "last_sync_at": r["last_sync_at"], "name": r["display_name"]}
    return out


@app.get("/api/sources")
def get_sources(user: dict = Depends(require_user)):
    """Every source this person is connected to or has data from, with what each one has.
    The app shows a picker when there is more than one."""
    caps = _capabilities(user["id"])
    conns = _connections(user["id"])
    result = []
    for source in SOURCES:
        if source not in caps and source not in conns:
            continue
        result.append({
            "source": source,
            "connected": source in conns,
            "status": conns.get(source, {}).get("status"),
            "last_sync_at": conns.get(source, {}).get("last_sync_at"),
            "has_data": source in caps,
            "available": {t: [c for c in v if c != "_rows"] for t, v in caps.get(source, {}).items()},
        })
    # default: the source whose data was stored most recently; else anything listed, else Garmin
    default = None
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT source FROM (
                SELECT source, MAX(created_at) AS t FROM activities WHERE user_id = %s GROUP BY source
                UNION ALL SELECT source, MAX(created_at) FROM daily_summary WHERE user_id = %s GROUP BY source
            ) x GROUP BY source ORDER BY MAX(t) DESC LIMIT 1
            """,
            (user["id"], user["id"]),
        )
        row = cur.fetchone()
        if row:
            default = row["source"]
    default = default or (result[0]["source"] if result else "garmin")
    return {"sources": result, "default": default}


def require_source(
    source: Optional[str] = Query(default=None),
    user: dict = Depends(require_user),
) -> dict:
    """The signed-in person plus which source this request is about (?source=).
    Without it the person's most recently updated source is used."""
    if source is None:
        source = get_sources(user)["default"]
    if source not in SOURCES:
        raise HTTPException(status_code=400, detail="unknown_source")
    return {**user, "source": source}


# ─── Data bounds ──────────────────────────────────────────────────────────────

@app.get("/api/range")
def get_range(user: dict = Depends(require_source)):
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                LEAST(
                    (SELECT MIN(date) FROM daily_summary WHERE steps IS NOT NULL AND user_id = %s AND source = %s),
                    (SELECT MIN(date) FROM sleep WHERE duration_seconds IS NOT NULL AND user_id = %s AND source = %s),
                    (SELECT MIN(start_time::date) FROM activities WHERE user_id = %s AND source = %s)
                ) AS earliest,
                GREATEST(
                    (SELECT MAX(date) FROM daily_summary WHERE steps IS NOT NULL AND user_id = %s AND source = %s),
                    (SELECT MAX(date) FROM sleep WHERE duration_seconds IS NOT NULL AND user_id = %s AND source = %s),
                    (SELECT MAX(start_time::date) FROM activities WHERE user_id = %s AND source = %s)
                ) AS latest
            """,
            (user["id"], user["source"]) * 6,
        )
        row = cur.fetchone()
    return {"earliest": row["earliest"], "latest": row["latest"]}


# ─── Daily summary ────────────────────────────────────────────────────────────

@app.get("/api/daily")
def get_daily(
    start: Optional[date] = Query(default=None),
    end:   Optional[date] = Query(default=None),
    user:  dict = Depends(require_source),
):
    if start is None or end is None:
        start, end = default_range()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT date, steps, step_goal, distance_meters,
                   active_calories, total_calories,
                   floors_ascended, floors_descended,
                   active_time_seconds, sedentary_seconds,
                   stress_avg, stress_rest,
                   body_battery_high, body_battery_low,
                   spo2_avg, spo2_min, hydration_ml,
                   resting_hr, min_hr_day, max_hr_day
            FROM daily_summary
            WHERE date BETWEEN %s AND %s AND user_id = %s AND source = %s
            ORDER BY date ASC
            """,
            (start, end, user["id"], user["source"]),
        )
        return cur.fetchall()


# ─── Sleep ────────────────────────────────────────────────────────────────────

@app.get("/api/sleep")
def get_sleep(
    start: Optional[date] = Query(default=None),
    end:   Optional[date] = Query(default=None),
    user:  dict = Depends(require_source),
):
    if start is None or end is None:
        start, end = default_range()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT date, start_time, end_time, duration_seconds,
                   light_seconds, deep_seconds, rem_seconds, awake_seconds,
                   sleep_score, avg_spo2, avg_respiration
            FROM sleep
            WHERE date BETWEEN %s AND %s AND user_id = %s AND source = %s
            ORDER BY date ASC
            """,
            (start, end, user["id"], user["source"]),
        )
        return cur.fetchall()


# ─── HRV ──────────────────────────────────────────────────────────────────────

@app.get("/api/hrv")
def get_hrv(
    start: Optional[date] = Query(default=None),
    end:   Optional[date] = Query(default=None),
    user:  dict = Depends(require_source),
):
    if start is None or end is None:
        start, end = default_range()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT date, hrv_weekly_avg, hrv_last_night,
                   hrv_last_night_5min, hrv_status
            FROM hrv
            WHERE date BETWEEN %s AND %s AND user_id = %s AND source = %s
            ORDER BY date ASC
            """,
            (start, end, user["id"], user["source"]),
        )
        return cur.fetchall()


# ─── Activities ───────────────────────────────────────────────────────────────

@app.get("/api/activities/map")
def get_activities_map(
    start: Optional[date] = Query(default=None),
    end:   Optional[date] = Query(default=None),
    user:  dict = Depends(require_source),
):
    if start is None or end is None:
        start, end = default_range()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT activity_id, start_time, activity_type, name,
                   duration_seconds, distance_meters, avg_hr, calories,
                   avg_pace_sec_per_km, elevation_gain_m,
                   start_lat, start_lng, end_lat, end_lng, polyline
            FROM activities
            WHERE start_time::date BETWEEN %s AND %s AND user_id = %s AND source = %s
            ORDER BY start_time DESC
            LIMIT 1000
            """,
            (start, end, user["id"], user["source"]),
        )
        return cur.fetchall()


@app.get("/api/activities")
def get_activities(
    start:         Optional[date] = Query(default=None),
    end:           Optional[date] = Query(default=None),
    activity_type: Optional[str]  = Query(default=None),
    user:          dict = Depends(require_source),
):
    if start is None or end is None:
        start, end = default_range()
    with db.cursor() as cur:
        if activity_type:
            cur.execute(
                """
                SELECT activity_id, start_time, activity_type, name,
                       duration_seconds, distance_meters, avg_hr, max_hr,
                       calories, avg_pace_sec_per_km, aerobic_te, anaerobic_te,
                       start_lat, start_lng, elevation_gain_m, avg_speed_mps, avg_cadence, avg_power
                FROM activities
                WHERE start_time::date BETWEEN %s AND %s AND user_id = %s AND source = %s AND activity_type = %s
                ORDER BY start_time ASC
                """,
                (start, end, user["id"], user["source"], activity_type.upper()),
            )
        else:
            cur.execute(
                """
                SELECT activity_id, start_time, activity_type, name,
                       duration_seconds, distance_meters, avg_hr, max_hr,
                       calories, avg_pace_sec_per_km, aerobic_te, anaerobic_te,
                       start_lat, start_lng, elevation_gain_m, avg_speed_mps, avg_cadence, avg_power
                FROM activities
                WHERE start_time::date BETWEEN %s AND %s AND user_id = %s AND source = %s
                ORDER BY start_time ASC
                """,
                (start, end, user["id"], user["source"]),
            )
        return cur.fetchall()


# ─── Touring ──────────────────────────────────────────────────────────────────

@app.get("/api/touring")
def get_touring(
    start: Optional[date] = Query(default=None),
    end:   Optional[date] = Query(default=None),
    user:  dict = Depends(require_source),
):
    if start is None or end is None:
        start, end = default_range()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT activity_id, start_time, activity_type, name,
                   duration_seconds, distance_meters, elevation_gain_m, avg_speed_mps,
                   avg_hr, avg_pace_sec_per_km, calories,
                   start_lat, start_lng, end_lat, end_lng,
                   polyline, weather_data, country_crossings, country
            FROM activities
            WHERE start_time::date BETWEEN %s AND %s
              AND user_id = %s AND source = %s
              AND polyline IS NOT NULL
              AND start_lat IS NOT NULL
            ORDER BY start_time ASC
            """,
            (start, end, user["id"], user["source"]),
        )
        activities = cur.fetchall()
        cur.execute(
            """
            SELECT date, start_time, end_time, duration_seconds, sleep_score, avg_spo2
            FROM sleep
            WHERE date BETWEEN %s AND %s AND user_id = %s AND source = %s
            ORDER BY date ASC
            """,
            (start, end, user["id"], user["source"]),
        )
        sleep = cur.fetchall()
    return {"activities": activities, "sleep": sleep}


# ─── Country statistics ───────────────────────────────────────────────────────

@app.get("/api/activities/countries")
def get_country_stats(
    start: Optional[date] = Query(default=None),
    end:   Optional[date] = Query(default=None),
    user:  dict = Depends(require_source),
):
    if start is None or end is None:
        start = date(2000, 1, 1)
        end   = date.today()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                country,
                activity_type,
                COUNT(*)                                                                   AS count,
                ROUND((COALESCE(SUM(distance_meters), 0) / 1000.0)::numeric, 1)::float   AS total_km,
                ROUND((COALESCE(SUM(duration_seconds), 0) / 3600.0)::numeric, 1)::float  AS total_hours,
                ROUND(COALESCE(SUM(elevation_gain_m), 0)::numeric)::int                  AS total_elevation_m,
                ROUND(AVG(avg_hr)::numeric)::int                                          AS avg_hr
            FROM activities
            WHERE start_time::date BETWEEN %s AND %s
              AND user_id = %s AND source = %s
              AND country IS NOT NULL
            GROUP BY country, activity_type
            ORDER BY country, total_km DESC NULLS LAST
            """,
            (start, end, user["id"], user["source"]),
        )
        rows = cur.fetchall()

    countries: dict = {}
    for row in rows:
        c = row["country"]
        if c not in countries:
            countries[c] = {"country": c, "total_activities": 0, "total_km": 0.0,
                            "total_hours": 0.0, "total_elevation_m": 0, "types": []}
        countries[c]["types"].append({
            "type": row["activity_type"], "count": row["count"],
            "total_km": row["total_km"], "total_hours": row["total_hours"],
            "total_elevation_m": row["total_elevation_m"], "avg_hr": row["avg_hr"],
        })
        countries[c]["total_activities"] += row["count"]
        countries[c]["total_km"]          = round(countries[c]["total_km"] + (row["total_km"] or 0), 1)
        countries[c]["total_hours"]       = round(countries[c]["total_hours"] + (row["total_hours"] or 0), 1)
        countries[c]["total_elevation_m"] += row["total_elevation_m"] or 0

    return sorted(countries.values(), key=lambda x: x["total_km"], reverse=True)


# ─── Summary ──────────────────────────────────────────────────────────────────

@app.get("/api/summary")
def get_summary(user: dict = Depends(require_source)):
    today = date.today()
    day7  = today - timedelta(days=7)
    day30 = today - timedelta(days=30)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT
                ROUND(AVG(steps))::int             AS avg_steps,
                ROUND(AVG(resting_hr))::int        AS avg_resting_hr,
                ROUND(AVG(body_battery_high))::int AS avg_bb_high,
                ROUND(AVG(body_battery_low))::int  AS avg_bb_low,
                ROUND(AVG(stress_avg))::int        AS avg_stress,
                ROUND(AVG(spo2_avg)::numeric, 1)::float AS avg_spo2
            FROM daily_summary
            WHERE date BETWEEN %s AND %s AND user_id = %s AND source = %s
            """,
            (day7, today, user["id"], user["source"]),
        )
        daily_7d = cur.fetchone()

        cur.execute(
            """
            SELECT
                ROUND(AVG(duration_seconds) / 3600.0, 2)::float AS avg_sleep_hours,
                ROUND(AVG(sleep_score))::int                     AS avg_sleep_score,
                ROUND(AVG(deep_seconds) / 60.0)::int             AS avg_deep_min,
                ROUND(AVG(rem_seconds) / 60.0)::int              AS avg_rem_min
            FROM sleep
            WHERE date BETWEEN %s AND %s AND user_id = %s AND source = %s
            """,
            (day7, today, user["id"], user["source"]),
        )
        sleep_7d = cur.fetchone()

        cur.execute(
            """
            SELECT
                ROUND(AVG(hrv_last_night))::int AS avg_hrv,
                ROUND(AVG(hrv_weekly_avg))::int AS avg_hrv_weekly
            FROM hrv
            WHERE date BETWEEN %s AND %s AND user_id = %s AND source = %s
            """,
            (day7, today, user["id"], user["source"]),
        )
        hrv_7d = cur.fetchone()

        cur.execute(
            "SELECT COUNT(*) AS count FROM activities WHERE start_time::date BETWEEN %s AND %s AND user_id = %s AND source = %s",
            (day7, today, user["id"], user["source"]),
        )
        activities_7d = cur.fetchone()

        cur.execute(
            "SELECT COUNT(*) AS count FROM activities WHERE start_time::date BETWEEN %s AND %s AND user_id = %s AND source = %s",
            (day30, today, user["id"], user["source"]),
        )
        activities_30d = cur.fetchone()

    return {
        "period_7d": {
            "daily":      daily_7d,
            "sleep":      sleep_7d,
            "hrv":        hrv_7d,
            "activities": activities_7d["count"],
        },
        "activities_30d": activities_30d["count"],
    }


# ─── Tours ────────────────────────────────────────────────────────────────────

class TourCreate(BaseModel):
    name: str
    description: Optional[str] = None
    activity_ids: List[int]


class TourUpdate(BaseModel):
    name: str
    description: Optional[str] = None


@app.get("/api/tours")
def list_tours(user: dict = Depends(require_source)):
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT t.id, t.name, t.description, t.created_at,
                   COUNT(ta.activity_id)::int AS activity_count,
                   ROUND((COALESCE(SUM(a.distance_meters), 0) / 1000.0)::numeric, 1)::float AS total_km,
                   MIN(a.start_time::date)::text AS start_date,
                   MAX(a.start_time::date)::text AS end_date
            FROM tours t
            LEFT JOIN tour_activities ta ON ta.tour_id = t.id
            LEFT JOIN activities a ON a.activity_id = ta.activity_id AND a.user_id = t.user_id AND a.source = t.source
            WHERE t.user_id = %s AND t.source = %s
            GROUP BY t.id
            ORDER BY t.created_at DESC
            """,
            (user["id"], user["source"],),
        )
        return cur.fetchall()


@app.post("/api/tours", status_code=201)
def create_tour(body: TourCreate, user: dict = Depends(require_source)):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO tours (name, description, user_id, source) VALUES (%s, %s, %s, %s) RETURNING id",
            (body.name, body.description, user["id"], user["source"]),
        )
        tour_id = cur.fetchone()["id"]
        for aid in body.activity_ids:
            cur.execute(
                "INSERT INTO tour_activities (tour_id, activity_id) VALUES (%s, %s)",
                (tour_id, aid),
            )
    return {"id": tour_id}


@app.get("/api/tours/{tour_id}")
def get_tour(tour_id: int, user: dict = Depends(require_source)):
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, name, description, created_at FROM tours WHERE id = %s AND user_id = %s AND source = %s",
            (tour_id, user["id"], user["source"]),
        )
        tour = cur.fetchone()
        if not tour:
            raise HTTPException(status_code=404, detail="Tour not found")

        cur.execute(
            """
            SELECT a.activity_id, a.start_time, a.activity_type, a.name,
                   a.duration_seconds, a.distance_meters, a.elevation_gain_m, a.avg_speed_mps,
                   a.avg_hr, a.avg_pace_sec_per_km, a.calories,
                   a.start_lat, a.start_lng, a.end_lat, a.end_lng,
                   a.polyline, a.weather_data, a.country_crossings, a.country
            FROM activities a
            JOIN tour_activities ta ON ta.activity_id = a.activity_id
            WHERE ta.tour_id = %s AND a.user_id = %s AND a.source = %s
            ORDER BY a.start_time ASC
            """,
            (tour_id, user["id"], user["source"]),
        )
        activities = cur.fetchall()

        if activities:
            dates = [a["start_time"].date() for a in activities]
            cur.execute(
                """
                SELECT date, start_time, end_time, duration_seconds, sleep_score, avg_spo2
                FROM sleep WHERE date BETWEEN %s AND %s AND user_id = %s AND source = %s ORDER BY date ASC
                """,
                (min(dates), max(dates), user["id"], user["source"]),
            )
            sleep = cur.fetchall()
        else:
            sleep = []

    return {"tour": tour, "activities": activities, "sleep": sleep}


@app.put("/api/tours/{tour_id}")
def update_tour(tour_id: int, body: TourUpdate, user: dict = Depends(require_source)):
    with db.cursor() as cur:
        cur.execute(
            "UPDATE tours SET name = %s, description = %s, updated_at = NOW() WHERE id = %s AND user_id = %s AND source = %s",
            (body.name, body.description, tour_id, user["id"], user["source"]),
        )
    return {"ok": True}


@app.delete("/api/tours/{tour_id}", status_code=204)
def delete_tour(tour_id: int, user: dict = Depends(require_source)):
    with db.cursor() as cur:
        cur.execute("DELETE FROM tours WHERE id = %s AND user_id = %s AND source = %s", (tour_id, user["id"], user["source"]))


# ─── Weather ──────────────────────────────────────────────────────────────────

@app.get("/api/weather-grid")
def get_weather_grid_region(
    min_lat: float, max_lat: float, min_lng: float, max_lng: float,
    start_date: str, end_date: str,
    user: dict = Depends(require_source),
):
    """Return ERA5 grid points for a bounding box and date range (used by touring)."""
    with db.cursor() as cur:
        cur.execute("""
            SELECT lat, lng, date::text AS date, hour, temperature_2m, precipitation,
                   wind_speed_10m, wind_direction_10m
            FROM weather_grid_points
            WHERE date BETWEEN %s AND %s
              AND lat  BETWEEN %s AND %s
              AND lng  BETWEEN %s AND %s
            ORDER BY date, lat, lng, hour
        """, (start_date, end_date, min_lat, max_lat, min_lng, max_lng))
        rows = cur.fetchall()
    return {"points": rows}


@app.get("/api/activities/{activity_id}/weather-grid")
def get_weather_grid(activity_id: int, user: dict = Depends(require_source)):
    """Return shared ERA5 grid points covering the activity's bounding box and date."""
    import json as _json
    with db.cursor() as cur:
        cur.execute(
            "SELECT polyline, start_time::date AS act_date FROM activities WHERE activity_id = %s AND user_id = %s AND source = %s",
            (activity_id, user["id"], user["source"]),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Activity not found")
    if not row["polyline"]:
        return {"points": []}

    pts  = row["polyline"] if isinstance(row["polyline"], list) else _json.loads(row["polyline"])
    lats = [p[0] for p in pts]
    lngs = [p[1] for p in pts]
    buf  = 0.6
    with db.cursor() as cur:
        cur.execute("""
            SELECT lat, lng, date::text AS date, hour, temperature_2m, precipitation,
                   wind_speed_10m, wind_direction_10m
            FROM weather_grid_points
            WHERE date = %s
              AND lat BETWEEN %s AND %s
              AND lng BETWEEN %s AND %s
            ORDER BY lat, lng, hour
        """, (row["act_date"], min(lats) - buf, max(lats) + buf, min(lngs) - buf, max(lngs) + buf))
        rows = cur.fetchall()
    return {"points": rows}


@app.get("/api/activities/{activity_id}/radar-timestamps")
def get_radar_timestamps(activity_id: int, user: dict = Depends(require_source)):
    """Return RainViewer timestamps stored for this activity."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT radar_timestamps FROM activities WHERE activity_id = %s AND user_id = %s AND source = %s",
            (activity_id, user["id"], user["source"]),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Activity not found")
    return {"timestamps": row["radar_timestamps"] or [], "zoom": 6}


@app.get("/api/radar/{timestamp}/{z}/{x}/{y}")
def get_radar_tile(timestamp: int, z: int, x: int, y: int):
    """Serve a stored radar tile PNG. No auth — weather data is non-sensitive."""
    STORED_ZOOM = 6
    # Map any requested zoom to stored zoom 6
    if z > STORED_ZOOM:
        factor = 2 ** (z - STORED_ZOOM)
        x, y = x // factor, y // factor
    elif z < STORED_ZOOM:
        factor = 2 ** (STORED_ZOOM - z)
        x, y = x * factor, y * factor
    z = STORED_ZOOM

    with db.cursor() as cur:
        cur.execute(
            "SELECT tile_data FROM weather_radar_tiles WHERE timestamp_unix=%s AND z=%s AND x=%s AND y=%s",
            (timestamp, z, x, y),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404)
    return Response(content=bytes(row["tile_data"]), media_type="image/png")
