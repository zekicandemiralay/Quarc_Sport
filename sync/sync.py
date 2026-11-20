"""
Quarc Sport — Garmin Connect → PostgreSQL sync service
──────────────────────────────────────────────────────
Multi-user: reads everyone who has saved Garmin credentials in the app (Settings →
Garmin Connect) and syncs each one independently every SYNC_INTERVAL_SECONDS.
Who a "user" is comes from quarc-auth; ids are its UUIDs (see db/init.sql).
"""

import os
import json
import time
import logging
import threading
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from garminconnect import Garmin, GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

import db
import strava
import weather as wx

try:
    import reverse_geocoder as rg
    HAS_GEOCODER = True
except ImportError:
    HAS_GEOCODER = False

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

BACKFILL_DAYS  = int(os.getenv("SYNC_BACKFILL_DAYS", 3650))
INTERVAL       = int(os.getenv("SYNC_INTERVAL_SECONDS", 3600))
GPS_BATCH_SIZE = int(os.getenv("GPS_BATCH_SIZE", 50))
TOKEN_ROOT     = Path(os.getenv("GARTH_TOKEN_ROOT", "/root/.garth"))

# Per-call Garmin client — set before each user's sync
client: Garmin = None


# ─── Fernet helpers ───────────────────────────────────────────────────────────

_fernet: Fernet | None = None

def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.getenv("CREDENTIALS_KEY", "")
        if not key:
            raise RuntimeError("CREDENTIALS_KEY env var is not set")
        _fernet = Fernet(key.encode())
    return _fernet

def decrypt_credential(ciphertext: bytes) -> str:
    return get_fernet().decrypt(bytes(ciphertext)).decode()


# ─── Per-user Garmin login ────────────────────────────────────────────────────

def get_token_dir(user_id: str) -> str:
    """Per-user garth token folder (OAuth tokens, so MFA accounts stay signed in)."""
    path = TOKEN_ROOT / user_id
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _execute(sql: str, params: tuple = ()) -> None:
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def set_status(user_id: str, status: str, error: str | None = None, retry_minutes: int | None = None) -> None:
    """Record how the Garmin connection is doing; the app shows it in Settings."""
    _execute(
        """
        UPDATE users SET garmin_status = %s, garmin_error = %s,
               retry_after = CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(mins => %s::int) END
        WHERE id = %s AND garmin_email_enc IS NOT NULL
        """,
        (status, error, retry_minutes, retry_minutes, user_id),
    )


def _garmin_login(email: str, password: str, token_dir: str) -> tuple[Garmin | None, bool]:
    """(client, needs_code). Uses the saved tokens; without usable ones, signs in
    with the email + password and keeps Garmin's new tokens."""
    garmin = Garmin(email=email, password=password, return_on_mfa=True)
    status, _ = garmin.login(tokenstore=token_dir)
    if status == "needs_mfa":
        return None, True
    if not garmin.display_name:
        # Signed in with the password (that mode skips the profile): save the
        # tokens, then load the profile through them.
        garmin.client.dump(token_dir)
        garmin = Garmin(email=email, password=password, return_on_mfa=True)
        garmin.login(tokenstore=token_dir)
    return garmin, False


def login_user(user: dict) -> Garmin | None:
    """Garmin client for one person, or None — then garmin_status says why
    (needs_code / auth_failed / error), so they can fix it themselves in the app."""
    user_id = user["id"]
    token_dir = get_token_dir(user_id)
    token_file = Path(token_dir) / "garmin_tokens.json"
    try:
        try:
            garmin, needs_code = _garmin_login(user["email"], user["password"], token_dir)
        except GarminConnectAuthenticationError:
            if not token_file.exists():
                raise
            # Saved tokens no longer accepted: try the email + password once.
            token_file.unlink(missing_ok=True)
            garmin, needs_code = _garmin_login(user["email"], user["password"], token_dir)
        if needs_code:
            set_status(user_id, "needs_code")
            log.warning(f"User {user_id}: Garmin asks for a two-factor code — waiting for them to reconnect in Settings")
            return None
        set_status(user_id, "connected")
        return garmin
    except GarminConnectAuthenticationError as e:
        set_status(user_id, "auth_failed", str(e)[:300])
        log.warning(f"User {user_id}: Garmin no longer accepts the saved login — {e}")
    except GarminConnectTooManyRequestsError as e:
        set_status(user_id, "error", "Garmin is rate-limiting sign-ins", retry_minutes=30)
        log.warning(f"User {user_id}: Garmin rate limit — retrying in 30 min ({e})")
    except Exception as e:
        set_status(user_id, "error", str(e)[:300], retry_minutes=15)
        log.warning(f"User {user_id}: Garmin login failed — retrying in 15 min ({e})")
    return None


# ─── Date helpers ──────────────────────────────────────────────────────────────

def _date_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def get_latest_synced_date(user_id: str) -> date | None:
    try:
        conn = db.get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT GREATEST(
                    (SELECT MAX(date) FROM daily_summary WHERE steps IS NOT NULL AND user_id = %s AND source = 'garmin'),
                    (SELECT MAX(date) FROM sleep WHERE duration_seconds IS NOT NULL AND user_id = %s AND source = 'garmin')
                )
            """, (user_id, user_id))
            row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def get_earliest_synced_date(user_id: str) -> date | None:
    try:
        conn = db.get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MIN(date) FROM daily_summary WHERE steps IS NOT NULL AND user_id = %s AND source = 'garmin'",
                (user_id,),
            )
            row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


# ─── Fetch helpers (use module-level `client`) ────────────────────────────────

def fetch_daily_summary(d: date) -> dict | None:
    ds = _date_str(d)
    try:
        s = client.get_user_summary(ds)
        if not s:
            return None
        return {
            "date":                 d,
            "steps":                s.get("totalSteps"),
            "step_goal":            s.get("dailyStepGoal"),
            "distance_meters":      s.get("totalDistanceMeters"),
            "active_calories":      s.get("activeKilocalories"),
            "total_calories":       s.get("totalKilocalories"),
            "floors_ascended":      s.get("floorsAscended"),
            "floors_descended":     s.get("floorsDescended"),
            "active_time_seconds":  s.get("activeSeconds"),
            "sedentary_seconds":    s.get("sedentarySeconds"),
            "stress_avg":           s.get("averageStressLevel"),
            "stress_rest":          s.get("restStressPercentage"),
            "body_battery_high":    s.get("bodyBatteryHighestValue"),
            "body_battery_low":     s.get("bodyBatteryLowestValue"),
            "spo2_avg":             s.get("averageSpo2"),
            "spo2_min":             s.get("lowestSpo2"),
            "hydration_ml":         s.get("totalFluidIntakeInOz"),
            "resting_hr":           s.get("restingHeartRate"),
            "min_hr_day":           s.get("minHeartRate"),
            "max_hr_day":           s.get("maxHeartRate"),
        }
    except Exception as e:
        log.warning(f"Daily summary fetch failed for {ds}: {e}")
        return None


def fetch_sleep(d: date) -> dict | None:
    ds = _date_str(d)
    try:
        data = client.get_sleep_data(ds)
        if not data or "dailySleepDTO" not in data:
            return None
        s = data["dailySleepDTO"]
        start_ms = s.get("sleepStartTimestampGMT")
        end_ms   = s.get("sleepEndTimestampGMT")
        score    = (s.get("sleepScores") or {}).get("overall", {}).get("value")
        return {
            "date":             d,
            "start_time":       datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc) if start_ms else None,
            "end_time":         datetime.fromtimestamp(end_ms   / 1000, tz=timezone.utc) if end_ms   else None,
            "duration_seconds": s.get("sleepTimeSeconds"),
            "light_seconds":    s.get("lightSleepSeconds"),
            "deep_seconds":     s.get("deepSleepSeconds"),
            "rem_seconds":      s.get("remSleepSeconds"),
            "awake_seconds":    s.get("awakeSleepSeconds"),
            "sleep_score":      score,
            "avg_spo2":         s.get("averageSpO2Value"),
            "avg_respiration":  s.get("averageRespirationValue"),
        }
    except Exception as e:
        log.warning(f"Sleep fetch failed for {ds}: {e}")
        return None


def fetch_hrv(d: date) -> dict | None:
    ds = _date_str(d)
    try:
        data = client.get_hrv_data(ds)
        if not data:
            return None
        summary = data.get("hrvSummary") or {}
        return {
            "date":                d,
            "hrv_weekly_avg":      summary.get("weeklyAvg"),
            "hrv_last_night":      summary.get("lastNight"),
            "hrv_last_night_5min": summary.get("lastNight5MinHigh"),
            "hrv_status":          summary.get("status"),
        }
    except Exception as e:
        log.warning(f"HRV fetch failed for {ds}: {e}")
        return None


def fetch_activities(start: date, end: date) -> list[dict]:
    try:
        activities = client.get_activities_by_date(_date_str(start), _date_str(end))
        if not activities:
            return []
        result = []
        for a in activities:
            dist     = a.get("distance")
            duration = a.get("duration")
            pace     = None
            if dist and duration and dist > 0:
                pace = duration / (dist / 1000)
            result.append({
                "activity_id":         a.get("activityId"),
                "start_time":          a.get("startTimeGMT"),
                "activity_type":       (a.get("activityType") or {}).get("typeKey", "").upper(),
                "name":                a.get("activityName"),
                "duration_seconds":    int(duration) if duration else None,
                "distance_meters":     dist,
                "avg_hr":              a.get("averageHR"),
                "max_hr":              a.get("maxHR"),
                "calories":            a.get("calories"),
                "avg_pace_sec_per_km": pace,
                "aerobic_te":          a.get("aerobicTrainingEffect"),
                "anaerobic_te":        a.get("anaerobicTrainingEffect"),
                "start_lat":           a.get("startLatitude"),
                "start_lng":           a.get("startLongitude"),
                "end_lat":             a.get("endLatitude"),
                "end_lng":             a.get("endLongitude"),
                "elevation_gain_m":    a.get("elevationGain"),
                "avg_speed_mps":       a.get("averageSpeed"),
                "avg_cadence":         a.get("averageRunningCadenceInStepsPerMinute") or a.get("averageCadence"),
                "avg_power":           a.get("averagePower"),
            })
        return result
    except Exception as e:
        log.warning(f"Activities fetch failed: {e}")
        return []


# ─── GPS helpers ──────────────────────────────────────────────────────────────

def extract_polyline(details: dict) -> list | None:
    descriptors = {d["metricsIndex"]: d["key"] for d in details.get("metricDescriptors", [])}
    lat_idx = next((i for i, k in descriptors.items() if k == "directLatitude"), None)
    lng_idx = next((i for i, k in descriptors.items() if k == "directLongitude"), None)
    spd_idx = next((i for i, k in descriptors.items() if k == "directSpeed"), None)
    if lat_idx is None or lng_idx is None:
        return None
    points = []
    for sample in details.get("activityDetailMetrics", []):
        m = sample.get("metrics", [])
        if lat_idx < len(m) and lng_idx < len(m):
            lat, lng = m[lat_idx], m[lng_idx]
            if lat and lng and abs(lat) > 0.001:
                spd = m[spd_idx] if (spd_idx is not None and spd_idx < len(m)) else None
                points.append([round(lat, 6), round(lng, 6), spd])
    if len(points) > 600:
        step = max(1, len(points) // 600)
        points = points[::step]
    return points if len(points) >= 3 else None


OUTDOOR_TYPES = (
    'RUNNING', 'TRAIL_RUNNING', 'CYCLING', 'MOUNTAIN_BIKING', 'VIRTUAL_RIDE',
    'HIKING', 'WALKING', 'SWIMMING', 'OPEN_WATER_SWIMMING', 'MULTISPORT',
    'TRIATHLON', 'RESORT_SKIING_SNOWBOARDING', 'BACKCOUNTRY_SKIING',
    'STAND_UP_PADDLEBOARDING', 'ROWING', 'OTHER',
    'GRAVEL_CYCLING', 'E_BIKE_RIDE', 'E_MOUNTAIN_BIKE_RIDE',   # Strava's cycling variants (their routes get countries/weather too)
)


# Activities whose details came back without a route (GPS off, indoor-ish
# "outdoor" types). Their polyline stays NULL, so without this they'd be fetched
# again on every pass and the same newest batch would block the older ones.
# Kept per process: after a restart each is tried once more.
_no_route: set[int] = set()


def gps_pending(conn, user_id: str) -> int:
    placeholders = ','.join(['%s'] * len(OUTDOOR_TYPES))
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COUNT(*) FROM activities
            WHERE polyline IS NULL AND start_lat IS NOT NULL AND user_id = %s AND source = 'garmin'
              AND activity_type IN ({placeholders}) AND NOT (activity_id = ANY(%s::bigint[]))
        """, (user_id, *OUTDOOR_TYPES, list(_no_route)))
        return cur.fetchone()[0]


def sync_missing_gps(conn, user_id: str, max_fetch: int = GPS_BATCH_SIZE) -> int:
    placeholders = ','.join(['%s'] * len(OUTDOOR_TYPES))
    total_pending = gps_pending(conn, user_id)
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT activity_id FROM activities
            WHERE polyline IS NULL AND start_lat IS NOT NULL AND user_id = %s AND source = 'garmin'
              AND activity_type IN ({placeholders}) AND NOT (activity_id = ANY(%s::bigint[]))
            ORDER BY start_time DESC LIMIT %s
        """, (user_id, *OUTDOOR_TYPES, list(_no_route), max_fetch))
        ids = [r[0] for r in cur.fetchall()]
    if not ids:
        log.info(f"User {user_id} GPS: all outdoor polylines up to date.")
        return 0
    remaining_after = max(0, total_pending - len(ids))
    log.info(f"User {user_id} GPS: fetching {len(ids)} polylines ({remaining_after} still pending) ...")
    stored = 0
    for aid in ids:
        try:
            details = client.get_activity_details(aid, maxpoly=4000)
            polyline = extract_polyline(details)
            db.upsert_activity_gps(conn, aid, polyline, user_id)
            if polyline:
                stored += 1
            else:
                _no_route.add(aid)
            time.sleep(0.5)
        except Exception as e:
            log.warning(f"GPS fetch failed for {aid}: {e}")
    log.info(f"User {user_id} GPS: stored {stored}/{len(ids)} — {remaining_after} still pending")
    return remaining_after


def sync_missing_coords(conn, user_id: str, chunk_days: int = 30):
    placeholders = ','.join(['%s'] * len(OUTDOOR_TYPES))
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT MIN(start_time::date), COUNT(*)
            FROM activities
            WHERE start_lat IS NULL AND user_id = %s AND source = 'garmin'
              AND activity_type IN ({placeholders})
        """, (user_id, *OUTDOOR_TYPES))
        row = cur.fetchone()
    if not row or not row[0]:
        return
    oldest_missing, total_missing = row[0], row[1]
    chunk_end = min(oldest_missing + timedelta(days=chunk_days - 1), date.today())
    log.info(f"User {user_id} Coords: {total_missing} missing — re-fetching {oldest_missing} → {chunk_end}")
    activity_rows = fetch_activities(oldest_missing, chunk_end)
    if activity_rows:
        db.upsert_activities(conn, activity_rows, user_id)


# ─── Sync range ───────────────────────────────────────────────────────────────

CHUNK_DAYS = 30


def sync_range(start: date, end: date, user_id: str, throttle: bool | None = None, on_chunk=None):
    """Fetch and store every day from start to end (oldest first, in CHUNK_DAYS
    pieces). throttle slows down to spare Garmin's rate limit (default: for long
    ranges); on_chunk(chunk_end) runs after each stored piece."""
    total_days = (end - start).days + 1
    is_large_backfill = total_days > 30 if throttle is None else throttle
    log.info(f"User {user_id}: syncing {start} → {end} ({total_days} days)...")

    chunk_start = start
    total_saved = {"daily": 0, "sleep": 0, "hrv": 0, "activities": 0}

    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), end)
        conn = db.get_conn()

        daily_rows, sleep_rows, hrv_rows = [], [], []
        d = chunk_start
        while d <= chunk_end:
            row = fetch_daily_summary(d)
            if row:
                daily_rows.append(row)
            row = fetch_sleep(d)
            if row:
                sleep_rows.append(row)
            row = fetch_hrv(d)
            if row:
                hrv_rows.append(row)
            d += timedelta(days=1)
            if is_large_backfill:
                time.sleep(0.4)

        activity_rows = fetch_activities(chunk_start, chunk_end)

        db.upsert_daily_summary(conn, daily_rows, user_id)
        db.upsert_sleep(conn, sleep_rows, user_id)
        db.upsert_hrv(conn, hrv_rows, user_id)
        db.upsert_activities(conn, activity_rows, user_id)
        conn.close()

        total_saved["daily"]      += len(daily_rows)
        total_saved["sleep"]      += len(sleep_rows)
        total_saved["hrv"]        += len(hrv_rows)
        total_saved["activities"] += len(activity_rows)
        if on_chunk:
            on_chunk(chunk_end)
        chunk_start = chunk_end + timedelta(days=1)

    log.info(
        f"User {user_id}: done — daily:{total_saved['daily']} sleep:{total_saved['sleep']} "
        f"hrv:{total_saved['hrv']} activities:{total_saved['activities']}"
    )


# ─── Enrichment ───────────────────────────────────────────────────────────────

def populate_missing_countries(conn, user_id: str):
    if not HAS_GEOCODER:
        return
    with conn.cursor() as cur:
        cur.execute("""
            SELECT activity_id, start_lat, start_lng FROM activities
            WHERE start_lat IS NOT NULL AND country IS NULL AND user_id = %s
            LIMIT 500
        """, (user_id,))
        rows = cur.fetchall()
    if not rows:
        return
    coords  = [(r[1], r[2]) for r in rows]
    results = rg.search(coords, mode=1)
    updates = [(res.get('cc', '??'), row[0]) for row, res in zip(rows, results)]
    with conn.cursor() as cur:
        for country_code, activity_id in updates:
            cur.execute(
                "UPDATE activities SET country = %s WHERE activity_id = %s AND user_id = %s",
                (country_code, activity_id, user_id),
            )
    conn.commit()
    log.info(f"User {user_id} Countries: tagged {len(updates)} activities")


def compute_country_crossings(conn, user_id: str):
    if not HAS_GEOCODER:
        return
    placeholders = ','.join(['%s'] * len(OUTDOOR_TYPES))
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT activity_id, polyline FROM activities
            WHERE polyline IS NOT NULL AND country_crossings IS NULL
              AND user_id = %s AND activity_type IN ({placeholders})
            LIMIT 50
        """, (user_id, *OUTDOOR_TYPES))
        rows = cur.fetchall()
    if not rows:
        return
    updated = 0
    for row in rows:
        try:
            polyline = row[1] if isinstance(row[1], list) else json.loads(row[1])
            if not polyline or len(polyline) < 2:
                crossings = []
            else:
                step    = max(1, len(polyline) // 60)
                samples = polyline[::step]
                coords  = [(p[0], p[1]) for p in samples]
                results = rg.search(coords, mode=1)
                crossings, prev_cc = [], None
                for pt, res in zip(samples, results):
                    cc = res.get("cc", "??")
                    if prev_cc and cc != prev_cc:
                        crossings.append({"lat": pt[0], "lng": pt[1], "from": prev_cc, "to": cc})
                    prev_cc = cc
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE activities SET country_crossings = %s WHERE activity_id = %s AND user_id = %s",
                    (json.dumps(crossings), row[0], user_id),
                )
            conn.commit()
            updated += 1
        except Exception as e:
            log.warning(f"Crossings failed for {row[0]}: {e}")
    if updated:
        log.info(f"User {user_id} Crossings: computed for {updated} activities")


WEATHER_FIELDS      = "temperature_2m,precipitation,wind_speed_10m,wind_direction_10m,weather_code,relative_humidity_2m"
ARCHIVE_CUTOFF_DAYS = 5


def _weather_url(lat: float, lng: float, ds: str, use_archive: bool) -> str:
    base = "archive-api.open-meteo.com/v1/archive" if use_archive else "api.open-meteo.com/v1/forecast"
    return (
        f"https://{base}"
        f"?latitude={lat:.4f}&longitude={lng:.4f}"
        f"&start_date={ds}&end_date={ds}"
        f"&hourly={WEATHER_FIELDS}"
        f"&timezone=auto"
    )


def fetch_weather_for_activities(conn, user_id: str):
    import requests as req
    archive_cutoff = date.today() - timedelta(days=ARCHIVE_CUTOFF_DAYS)
    placeholders   = ','.join(['%s'] * len(OUTDOOR_TYPES))
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT activity_id, start_time::date, start_lat, start_lng
            FROM activities
            WHERE start_lat IS NOT NULL AND weather_data IS NULL AND user_id = %s
              AND activity_type IN ({placeholders})
            ORDER BY start_time DESC LIMIT 20
        """, (user_id, *OUTDOOR_TYPES))
        rows = cur.fetchall()
    if not rows:
        return
    updated = 0
    for row in rows:
        try:
            act_date    = row[1]
            ds          = str(act_date)
            use_archive = act_date <= archive_cutoff
            url         = _weather_url(row[2], row[3], ds, use_archive)
            resp        = req.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json().get("hourly", {})
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE activities SET weather_data = %s WHERE activity_id = %s AND user_id = %s",
                        (json.dumps(data), row[0], user_id),
                    )
                conn.commit()
                updated += 1
            elif resp.status_code == 400 and not use_archive:
                pass
            time.sleep(0.3)
        except Exception as e:
            log.warning(f"Weather fetch failed for activity {row[0]}: {e}")
    if updated:
        log.info(f"User {user_id} Weather: fetched data for {updated} activities")


# ─── Scheduling ───────────────────────────────────────────────────────────────
#
# One small job at a time, re-planned after each, so nobody waits behind someone
# else's ten-year import:
#   1. recent  — someone just connected / pressed Sync now (oldest request first)
#   2. recent  — hourly update for anyone whose last sync is older than INTERVAL
#   3. history — one HISTORY_CHUNK_DAYS piece of older history, newest first
#   4. gps     — a batch of routes for activities that don't have one yet
# A new person's first job imports the latest RECENT_DAYS, so their dashboard
# fills within a minute or two; older history follows in the background.

POLL_SECONDS       = int(os.getenv("SYNC_POLL_SECONDS", 10))
RECENT_DAYS        = 30
HISTORY_CHUNK_DAYS = 30
RESYNC_DAYS        = 7      # Garmin keeps updating the last few days (sleep, HRV, late uploads)
GPS_RETRY_SECONDS  = 6 * 3600


def next_job(candidates: list[dict], now: datetime, gps_wait_until: dict | None = None) -> tuple[dict, str] | None:
    """Pick the next (person, kind) to work on. Pure, so it is unit-tested."""
    gps_wait_until = gps_wait_until or {}
    requested = [c for c in candidates if c.get("sync_requested_at")]
    if requested:
        return min(requested, key=lambda c: c["sync_requested_at"]), "recent"
    due = [c for c in candidates
           if c.get("last_sync_at") is None or (now - c["last_sync_at"]).total_seconds() >= INTERVAL]
    if due:
        never = datetime.min.replace(tzinfo=timezone.utc)
        return min(due, key=lambda c: c.get("last_sync_at") or never), "recent"
    history = [c for c in candidates
               if c.get("history_from") and c.get("history_target") and c["history_from"] > c["history_target"]]
    if history:
        # whoever has the least history so far
        return max(history, key=lambda c: c["history_from"]), "history"
    routes = [c for c in candidates if c.get("gps_pending") and gps_wait_until.get(c["id"], 0) <= now.timestamp()]
    if routes:
        return routes[0], "gps"
    return None


def first_import_plan(today: date, latest_stored: date | None) -> tuple[date, date]:
    """(start, history_target) for someone's first job. Days an earlier version
    already imported (it went oldest → newest, up to latest_stored) count as done."""
    start = today - timedelta(days=RECENT_DAYS - 1)
    target = latest_stored + timedelta(days=1) if latest_stored else today - timedelta(days=BACKFILL_DAYS)
    return start, min(target, start)


def load_candidates() -> list[dict]:
    """Everyone with a working Garmin login (or a temporary error whose wait is over)."""
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, garmin_email_enc, garmin_pass_enc, sync_requested_at, last_sync_at,
                       synced_until, history_from, history_target,
                       EXISTS (SELECT 1 FROM activities a
                               WHERE a.user_id = users.id AND a.source = 'garmin' AND a.polyline IS NULL AND a.start_lat IS NOT NULL
                                 AND a.activity_type IN ({','.join(['%s'] * len(OUTDOOR_TYPES))})
                                 AND NOT (a.activity_id = ANY(%s::bigint[])))
                FROM users
                WHERE garmin_email_enc IS NOT NULL AND garmin_pass_enc IS NOT NULL
                  AND COALESCE(garmin_status, 'connected') IN ('connected', 'error')
                  AND (retry_after IS NULL OR retry_after <= now())
                """,
                (*OUTDOOR_TYPES, list(_no_route)),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    keys = ("id", "email_enc", "pass_enc", "sync_requested_at", "last_sync_at",
            "synced_until", "history_from", "history_target", "gps_pending")
    return [dict(zip(keys, r)) for r in rows]


def _update_user(user_id: str, **fields) -> None:
    """Move the import cursor (synced_until, history_from, history_target); last_sync=True also stamps last_sync_at."""
    sets, params = [], []
    for name in ("synced_until", "history_from", "history_target"):
        if name in fields:
            sets.append(f"{name} = %s")
            params.append(fields[name])
    if fields.get("last_sync"):
        sets.append("last_sync_at = now()")
    if sets:
        _execute(f"UPDATE users SET {', '.join(sets)} WHERE id = %s", (*params, user_id))


def run_recent(user: dict) -> None:
    user_id, today = user["id"], date.today()
    if user["synced_until"] is None:
        start, target = first_import_plan(today, get_latest_synced_date(user_id))
        log.info(f"User {user_id}: first import — latest {RECENT_DAYS} days now, then history back to {target}")
        sync_range(start, today, user_id, throttle=False)
        _update_user(user_id, synced_until=today, history_from=start, history_target=target, last_sync=True)
    else:
        start = user["synced_until"] - timedelta(days=RESYNC_DAYS - 1)
        sync_range(start, today, user_id, on_chunk=lambda end: _update_user(user_id, synced_until=end))
        _update_user(user_id, synced_until=today, last_sync=True)


def run_history(user: dict) -> None:
    user_id = user["id"]
    end = user["history_from"] - timedelta(days=1)
    start = max(user["history_target"], end - timedelta(days=HISTORY_CHUNK_DAYS - 1))
    log.info(f"User {user_id}: history {start} → {end} (back to {user['history_target']})")
    sync_range(start, end, user_id, throttle=True)
    _update_user(user_id, history_from=start)


def enrich(user_id: str) -> None:
    """Routes, countries, border crossings and weather for what was just stored (each bounded)."""
    conn = db.get_conn()
    try:
        sync_missing_coords(conn, user_id)
        sync_missing_gps(conn, user_id)
        populate_missing_countries(conn, user_id)
        compute_country_crossings(conn, user_id)
        fetch_weather_for_activities(conn, user_id)
    finally:
        conn.close()


_gps_wait_until: dict[str, float] = {}


def run_job(user: dict, kind: str) -> None:
    global client
    user_id = user["id"]
    _execute(
        "UPDATE users SET syncing_since = now(), "
        "sync_requested_at = CASE WHEN %s THEN NULL ELSE sync_requested_at END WHERE id = %s",
        (kind == "recent", user_id),
    )
    try:
        try:
            user["email"] = decrypt_credential(user["email_enc"])
            user["password"] = decrypt_credential(user["pass_enc"])
        except Exception as e:
            set_status(user_id, "error", f"cannot decrypt the saved login (CREDENTIALS_KEY changed?): {e}", retry_minutes=60)
            return
        garmin = login_user(user)
        if garmin is None:
            return
        client = garmin
        if kind == "recent":
            run_recent(user)
        elif kind == "history":
            run_history(user)
        before = None
        if kind == "gps":
            conn = db.get_conn()
            before = gps_pending(conn, user_id)
            conn.close()
        enrich(user_id)
        if kind == "gps":
            conn = db.get_conn()
            after = gps_pending(conn, user_id)
            conn.close()
            # no progress (Garmin returned nothing new): leave routes for a while
            _gps_wait_until[user_id] = time.time() + (GPS_RETRY_SECONDS if after >= before else 0)
    except Exception as e:
        log.error(f"User {user_id}: {kind} sync failed: {e}", exc_info=True)
        set_status(user_id, "error", str(e)[:300], retry_minutes=15)
    finally:
        _execute("UPDATE users SET syncing_since = NULL WHERE id = %s", (user_id,))


# ─── Weather background loops ─────────────────────────────────────────────────

RADAR_INTERVAL = int(os.getenv("RADAR_CHECK_INTERVAL_SECONDS", 900))   # 15 min default
ERA5_INTERVAL  = int(os.getenv("ERA5_CHECK_INTERVAL_SECONDS",  1800))  # 30 min default


def _radar_loop():
    """Download RainViewer radar tiles for recent activities. Runs every 15 min."""
    while True:
        try:
            conn = db.get_conn()
            frames = wx.fetch_rainviewer_frames()
            if not frames:
                log.info("Radar: no frames available from RainViewer")
                conn.close()
                time.sleep(RADAR_INTERVAL)
                continue

            activities = db.get_recent_activities_for_radar(conn, sync_window_hours=wx.RADAR_WINDOW_HOURS)
            if not activities:
                conn.close()
                time.sleep(RADAR_INTERVAL)
                continue

            log.info(f"Radar: checking {len(activities)} recent activities against {len(frames)} frames")
            total_tiles = 0
            for act in activities:
                try:
                    polyline = act["polyline"] if isinstance(act["polyline"], list) else json.loads(act["polyline"])
                    if not polyline:
                        continue

                    start_time = act["start_time"]
                    if start_time.tzinfo is None:
                        start_time = start_time.replace(tzinfo=timezone.utc)

                    matching = wx.frames_for_activity(frames, start_time, act["duration_seconds"])
                    if not matching:
                        # No radar coverage — still mark so we don't retry forever
                        db.update_activity_radar_timestamps(conn, act["user_id"], act["activity_id"], [])
                        continue

                    tiles = wx.tiles_for_polyline(polyline, wx.RADAR_ZOOM)
                    saved_ts = []
                    for frame in matching:
                        ts_saved = True
                        for (x, y) in tiles:
                            if db.radar_tile_exists(conn, frame["timestamp"], wx.RADAR_ZOOM, x, y):
                                continue
                            data = wx.download_tile(frame["host"], frame["path"], wx.RADAR_ZOOM, x, y)
                            if data:
                                db.upsert_radar_tile(conn, frame["timestamp"], wx.RADAR_ZOOM, x, y, data)
                                total_tiles += 1
                            else:
                                ts_saved = False
                            time.sleep(0.1)
                        if ts_saved:
                            saved_ts.append(frame["timestamp"])

                    db.update_activity_radar_timestamps(conn, act["user_id"], act["activity_id"], saved_ts)
                except Exception as e:
                    log.warning(f"Radar: activity {act['activity_id']} failed: {e}")

            if total_tiles:
                log.info(f"Radar: downloaded {total_tiles} new tiles")
            conn.close()
        except Exception as e:
            log.error(f"Radar loop error: {e}", exc_info=True)
        time.sleep(RADAR_INTERVAL)


def _era5_loop():
    """Fetch ERA5 grid for activities that don't have it yet. Runs every 30 min."""
    while True:
        try:
            conn = db.get_conn()
            activities = db.get_activities_needing_era5(conn, limit=8)
            if activities:
                log.info(f"ERA5: processing {len(activities)} activities")
            for act in activities:
                try:
                    polyline = act["polyline"] if isinstance(act["polyline"], list) else json.loads(act["polyline"])
                    if not polyline or len(polyline) < 2:
                        db.mark_activity_grid_fetched(conn, act["user_id"], act["activity_id"])
                        continue

                    min_lat, max_lat, min_lng, max_lng = wx.grid_bbox(polyline)

                    # Check if another activity already populated this area
                    if db.grid_exists_for_area(conn, min_lat, max_lat, min_lng, max_lng, act["date"]):
                        db.mark_activity_grid_fetched(conn, act["user_id"], act["activity_id"])
                        continue

                    grid_data = wx.fetch_era5_grid(polyline, act["date"])
                    rows = wx.era5_to_rows(grid_data, act["date"])
                    if rows:
                        db.upsert_weather_grid(conn, rows)
                        log.info(f"ERA5: stored {len(rows)} grid rows for activity {act['activity_id']}")
                    db.mark_activity_grid_fetched(conn, act["user_id"], act["activity_id"])
                except Exception as e:
                    log.warning(f"ERA5: activity {act['activity_id']} failed: {e}")
            conn.close()
        except Exception as e:
            log.error(f"ERA5 loop error: {e}", exc_info=True)
        time.sleep(ERA5_INTERVAL)


# ─── Main ─────────────────────────────────────────────────────────────────────

def enrich_common(user_id: str) -> None:
    """Countries, border crossings and weather for whatever sources stored for this person
    (no Garmin calls: used by the Strava job)."""
    conn = db.get_conn()
    try:
        populate_missing_countries(conn, user_id)
        compute_country_crossings(conn, user_id)
        fetch_weather_for_activities(conn, user_id)
    finally:
        conn.close()



def main():
    db.ensure_schema()
    log.info("Schema check done.")

    # Start weather background threads
    threading.Thread(target=_radar_loop, daemon=True, name="radar").start()
    threading.Thread(target=_era5_loop,  daemon=True, name="era5").start()
    log.info("Weather threads started (radar every 15min, ERA5 every 30min)")

    # A job cut short by a restart leaves its "syncing" flag behind.
    _execute("UPDATE users SET syncing_since = NULL WHERE syncing_since IS NOT NULL")
    _execute("UPDATE source_connections SET syncing_since = NULL WHERE syncing_since IS NOT NULL")

    # Strava has its own loop (its own tokens, rate limits and pace); it never touches Garmin's.
    threading.Thread(target=strava.loop, args=(enrich_common,), daemon=True, name="strava").start()
    log.info(f"Waiting for work (checking every {POLL_SECONDS}s; hourly updates, history newest first).")

    idle = False
    while True:
        try:
            job = next_job(load_candidates(), datetime.now(timezone.utc), _gps_wait_until)
        except Exception as e:
            log.warning(f"Could not read the job list: {e}")
            job = None
        if job is None:
            if not idle:
                log.info("Nothing to do — everyone is up to date.")
                idle = True
            time.sleep(POLL_SECONDS)
            continue
        idle = False
        user, kind = job
        log.info(f"--- User {user['id']}: {kind} ---")
        if kind == "gps":
            _gps_wait_until[user["id"]] = time.time() + GPS_RETRY_SECONDS  # until the job says otherwise
        run_job(user, kind)


if __name__ == "__main__":
    main()
