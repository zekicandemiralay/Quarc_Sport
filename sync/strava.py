"""
Strava → PostgreSQL.

Strava is its own source ('strava'): its activities are stored with source = 'strava' and are
never merged with Garmin's or the phone's. Only what Strava reports is stored — no calories,
scores or daily summaries are made up (Strava has activities, nothing else).

How it works: each person connects in the app (OAuth, see api/strava_api.py), which stores their
tokens encrypted in source_connections. This module's loop then
  - imports their whole history, newest first, a few pages per pass so nobody waits for anybody,
  - and every STRAVA_SYNC_INTERVAL_SECONDS re-reads the last RECENT_DAYS days (new uploads, edits).
It respects Strava's rate limits (the usage headers, and 429 → wait).
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from cryptography.fernet import Fernet
from psycopg2.extras import execute_values

import db

log = logging.getLogger("strava")

API_BASE = os.getenv("STRAVA_API_BASE", "https://www.strava.com/api/v3").rstrip("/")
OAUTH_BASE = os.getenv("STRAVA_OAUTH_BASE", "https://www.strava.com/oauth").rstrip("/")

PAGE_SIZE = 100
PAGES_PER_JOB = int(os.getenv("STRAVA_PAGES_PER_JOB", 5))      # ≤ 500 activities, then the next person gets a turn
RECENT_DAYS = 7
INTERVAL = int(os.getenv("STRAVA_SYNC_INTERVAL_SECONDS", 1800))
POLL_SECONDS = int(os.getenv("SYNC_POLL_SECONDS", 10))
MAX_POLYLINE_POINTS = 600
HTTP_TIMEOUT = 30


class StravaAuthError(Exception):
    """Strava no longer accepts this person's access (revoked, or the app was removed)."""


class StravaRateLimited(Exception):
    def __init__(self, minutes: int):
        super().__init__(f"rate limited, retry in {minutes} min")
        self.minutes = minutes


class StravaError(Exception):
    """Temporary trouble (network, 5xx)."""


# ─── Credentials (encrypted at rest, like Garmin's) ───────────────────────────

def _fernet() -> Fernet:
    return Fernet(os.environ["CREDENTIALS_KEY"].encode())


def load_credentials(enc: bytes) -> dict:
    return json.loads(_fernet().decrypt(bytes(enc)).decode())


def dump_credentials(creds: dict) -> bytes:
    return _fernet().encrypt(json.dumps(creds).encode())


def refresh_tokens(creds: dict, app: dict) -> dict:
    """Strava access tokens last 6 hours; the refresh token is rotated every time.
    `app` is the person's own Strava app: {client_id, client_secret}."""
    try:
        r = requests.post(
            f"{OAUTH_BASE}/token",
            data={
                "client_id": app["client_id"],
                "client_secret": app["client_secret"],
                "grant_type": "refresh_token",
                "refresh_token": creds["refresh_token"],
            },
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as e:
        raise StravaError(f"token refresh failed: {e}")
    if r.status_code in (400, 401, 403):
        raise StravaAuthError("Strava refused the saved access — reconnect Strava")
    if not r.ok:
        raise StravaError(f"token refresh: HTTP {r.status_code}")
    d = r.json()
    return {"access_token": d["access_token"], "refresh_token": d["refresh_token"], "expires_at": d["expires_at"]}


# ─── API client ───────────────────────────────────────────────────────────────

def _next_utc_midnight_minutes() -> int:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds() // 60))


class Api:
    def __init__(self, access_token: str):
        self.token = access_token
        self.near_limit = False

    def get(self, path: str, params: dict | None = None):
        try:
            r = requests.get(f"{API_BASE}{path}", params=params, headers={"Authorization": f"Bearer {self.token}"},
                             timeout=HTTP_TIMEOUT)
        except requests.RequestException as e:
            raise StravaError(str(e))
        self._note_usage(r)
        if r.status_code == 401:
            raise StravaAuthError("Strava does not accept the access token")
        if r.status_code == 429:
            raise StravaRateLimited(self._retry_minutes(r))
        if not r.ok:
            raise StravaError(f"HTTP {r.status_code} on {path}")
        return r.json()

    @staticmethod
    def _limits(r) -> tuple[list[int], list[int]]:
        def nums(h):
            try:
                return [int(x) for x in (r.headers.get(h) or "").split(",")]
            except ValueError:
                return []
        return nums("X-RateLimit-Limit"), nums("X-RateLimit-Usage")

    def _note_usage(self, r) -> None:
        limit, usage = self._limits(r)
        if len(limit) == 2 and len(usage) == 2 and (usage[0] >= limit[0] - 3 or usage[1] >= limit[1] - 3):
            self.near_limit = True

    def _retry_minutes(self, r) -> int:
        limit, usage = self._limits(r)
        if len(limit) == 2 and len(usage) == 2 and usage[1] >= limit[1]:
            return _next_utc_midnight_minutes()      # the daily budget is spent
        return 16                                    # the 15-minute window resets on the quarter hour


# ─── Mapping (pure) ───────────────────────────────────────────────────────────

def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    """Google's encoded polyline algorithm (precision 5), as Strava uses it."""
    coords, index, lat, lng = [], 0, 0, 0
    while index < len(encoded):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lng += delta
        coords.append((lat / 1e5, lng / 1e5))
    return coords


def downsample(points: list, limit: int = MAX_POLYLINE_POINTS) -> list:
    if len(points) <= limit:
        return points
    step = -(-len(points) // limit)        # rounded up, so the result never exceeds the limit
    sampled = points[::step]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


# Strava sport types → the activity-type keys the app already knows (and has translations for).
TYPE_MAP = {
    "Run": "RUNNING", "TrailRun": "TRAIL_RUNNING", "VirtualRun": "VIRTUAL_RUN",
    "Walk": "WALKING", "Hike": "HIKING",
    "Ride": "CYCLING", "GravelRide": "GRAVEL_CYCLING", "MountainBikeRide": "MOUNTAIN_BIKING",
    "EBikeRide": "E_BIKE_RIDE", "EMountainBikeRide": "E_MOUNTAIN_BIKE_RIDE", "VirtualRide": "VIRTUAL_RIDE",
    "Swim": "SWIMMING", "WeightTraining": "STRENGTH_TRAINING", "Yoga": "YOGA",
}


def map_type(a: dict) -> str:
    kind = a.get("sport_type") or a.get("type") or "Workout"
    if kind in TYPE_MAP:
        return TYPE_MAP[kind]
    out = ""
    for i, ch in enumerate(kind):                     # CamelCase → UPPER_SNAKE
        if ch.isupper() and i and not kind[i - 1].isupper():
            out += "_"
        out += ch.upper()
    return out[:50]


def _parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def map_activity(a: dict) -> dict | None:
    """One Strava SummaryActivity → a row of our activities table. Only what Strava reports."""
    if not a.get("id") or not a.get("start_date"):
        return None
    dist, moving = a.get("distance"), a.get("moving_time")
    start_ll, end_ll = a.get("start_latlng") or [], a.get("end_latlng") or []

    polyline = None
    encoded = (a.get("map") or {}).get("summary_polyline")
    if encoded:
        pts = downsample(decode_polyline(encoded))
        if len(pts) >= 3:
            polyline = [[round(lat, 5), round(lng, 5)] for lat, lng in pts]

    def whole(v):
        return int(round(v)) if v is not None else None

    speed = a.get("average_speed")
    return {
        "activity_id": int(a["id"]),
        "start_time": _parse_time(a["start_date"]),
        "activity_type": map_type(a),
        "name": (a.get("name") or "")[:200],
        "duration_seconds": whole(moving),
        "distance_meters": dist if dist else None,
        "avg_hr": whole(a.get("average_heartrate")),
        "max_hr": whole(a.get("max_heartrate")),
        "avg_pace_sec_per_km": round(moving / (dist / 1000), 1) if dist and moving and dist > 0 else None,
        "start_lat": start_ll[0] if len(start_ll) == 2 else None,
        "start_lng": start_ll[1] if len(start_ll) == 2 else None,
        "end_lat": end_ll[0] if len(end_ll) == 2 else None,
        "end_lng": end_ll[1] if len(end_ll) == 2 else None,
        "elevation_gain_m": a.get("total_elevation_gain"),
        "avg_speed_mps": speed if speed else None,
        "avg_cadence": whole(a.get("average_cadence")),
        "avg_power": whole(a.get("average_watts")),
        "polyline": polyline,
    }


# ─── Storing ──────────────────────────────────────────────────────────────────

ROW_COLUMNS = [
    "activity_id", "start_time", "activity_type", "name", "duration_seconds", "distance_meters", "avg_hr", "max_hr",
    "avg_pace_sec_per_km", "start_lat", "start_lng", "end_lat", "end_lng", "elevation_gain_m", "avg_speed_mps",
    "avg_cadence", "avg_power", "polyline",
]


def upsert_activities(conn, user_id: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = ["user_id", "source"] + ROW_COLUMNS
    template = "(" + ", ".join("%s::jsonb" if c == "polyline" else "%s" for c in cols) + ")"
    values = [
        (user_id, "strava", *[json.dumps(r[c]) if c == "polyline" and r[c] is not None else r[c] for c in ROW_COLUMNS])
        for r in rows
    ]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in ROW_COLUMNS if c != "activity_id")
    sql = (f"INSERT INTO activities ({', '.join(cols)}) VALUES %s "
           f"ON CONFLICT (user_id, source, activity_id) DO UPDATE SET {updates}")
    with conn.cursor() as cur:
        execute_values(cur, sql, values, template=template)
    conn.commit()


NOW = object()   # in _update(): "= now()"


def _update(user_id: str, **fields) -> None:
    """Set columns on this person's Strava connection; NOW stands for SQL now()."""
    sets, params = [], []
    for k, v in fields.items():
        if v is NOW:
            sets.append(f"{k} = now()")
        else:
            sets.append(f"{k} = %s")
            params.append(v)
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE source_connections SET {', '.join(sets)} WHERE user_id = %s AND source = 'strava'",
                        (*params, user_id))
        conn.commit()
    finally:
        conn.close()



def _count(user_id: str) -> int:
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM activities WHERE user_id = %s AND source = 'strava'", (user_id,))
            return cur.fetchone()[0]
    finally:
        conn.close()


# ─── Jobs ─────────────────────────────────────────────────────────────────────

def load_candidates() -> list[dict]:
    """Connected people whose wait (if any) is over."""
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id, credentials_enc, app_credentials_enc, sync_requested_at, last_sync_at, history_done, history_before, history_page
                FROM source_connections
                WHERE source = 'strava' AND credentials_enc IS NOT NULL
                  AND status IN ('connected', 'error')
                  AND (retry_after IS NULL OR retry_after <= now())
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    keys = ("user_id", "credentials_enc", "app_credentials_enc", "sync_requested_at", "last_sync_at", "history_done", "history_before", "history_page")
    return [dict(zip(keys, r)) for r in rows]


def next_job(candidates: list[dict], now: datetime) -> dict | None:
    """Who to work on next. Pure, so it is unit-tested."""
    requested = [c for c in candidates if c.get("sync_requested_at")]
    if requested:
        return min(requested, key=lambda c: c["sync_requested_at"])
    importing = [c for c in candidates if not c.get("history_done")]
    if importing:
        never = datetime.min.replace(tzinfo=timezone.utc)
        return min(importing, key=lambda c: c.get("last_sync_at") or never)   # whoever waited longest
    due = [c for c in candidates if c.get("last_sync_at") is None or (now - c["last_sync_at"]).total_seconds() >= INTERVAL]
    if due:
        never = datetime.min.replace(tzinfo=timezone.utc)
        return min(due, key=lambda c: c.get("last_sync_at") or never)
    return None


def _fetch_pages(api: Api, params: dict, first_page: int, max_pages: int):
    """Yield (page_number, [activities]) until an empty or short page, or max_pages."""
    page = first_page
    for _ in range(max_pages):
        acts = api.get("/athlete/activities", {**params, "per_page": PAGE_SIZE, "page": page})
        yield page, acts
        if len(acts) < PAGE_SIZE or api.near_limit:
            return
        page += 1


def run_job(candidate: dict, enrich=None) -> None:
    uid = candidate["user_id"]
    _update(uid, syncing_since=NOW, sync_requested_at=None)
    try:
        creds = load_credentials(candidate["credentials_enc"])
        if creds["expires_at"] - time.time() < 300:
            if not candidate.get("app_credentials_enc"):
                raise StravaAuthError("the Strava app credentials are missing — enter them again")
            creds = refresh_tokens(creds, load_credentials(candidate["app_credentials_enc"]))
            _update(uid, credentials_enc=dump_credentials(creds))
        api = Api(creds["access_token"])
        conn = db.get_conn()
        try:
            if not candidate["history_done"]:
                _import_history(api, conn, candidate)
            else:
                _import_recent(api, conn, uid)
        finally:
            conn.close()
        if enrich:
            enrich(uid)
        _update(uid, status="connected", error=None, last_sync_at=NOW, items_synced=_count(uid),
                retry_after=(datetime.now(timezone.utc) + timedelta(minutes=16)) if api.near_limit else None)
    except StravaAuthError as e:
        log.warning("Strava user %s: access no longer valid: %s", uid, e)
        _update(uid, status="auth_failed", error=str(e)[:300], retry_after=None)
    except StravaRateLimited as e:
        log.warning("Strava user %s: %s", uid, e)
        _update(uid, retry_after=datetime.now(timezone.utc) + timedelta(minutes=e.minutes))
    except Exception as e:
        log.error("Strava user %s: sync failed: %s", uid, e, exc_info=True)
        _update(uid, status="error", error=str(e)[:300], retry_after=datetime.now(timezone.utc) + timedelta(minutes=15))
    finally:
        _update(uid, syncing_since=None)


def _import_history(api: Api, conn, c: dict) -> None:
    uid = c["user_id"]
    # Pages are counted against a fixed cutoff, so activities uploaded meanwhile can't shift them.
    cutoff = c["history_before"] or datetime.now(timezone.utc)
    if c["history_before"] is None:
        _update(uid, history_before=cutoff)
    page = c["history_page"] or 1
    for page, acts in _fetch_pages(api, {"before": int(cutoff.timestamp())}, page, PAGES_PER_JOB):
        rows = [r for r in (map_activity(a) for a in acts) if r]
        upsert_activities(conn, uid, rows)
        if len(acts) < PAGE_SIZE:
            _update(uid, history_done=True, history_page=page, items_synced=_count(uid))
            log.info("Strava user %s: history imported", uid)
            return
        _update(uid, history_page=page + 1, items_synced=_count(uid))   # progress shows in the app
    log.info("Strava user %s: history continues at page %s", uid, page + 1)


def _import_recent(api: Api, conn, uid: str) -> None:
    after = int((datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).timestamp())
    for _page, acts in _fetch_pages(api, {"after": after}, 1, 20):
        upsert_activities(conn, uid, [r for r in (map_activity(a) for a in acts) if r])


def loop(enrich=None) -> None:
    """Runs forever in a thread of the sync service."""
    log.info("Strava sync started (every %ss, history %s pages per pass)", INTERVAL, PAGES_PER_JOB)
    while True:
        try:
            job = next_job(load_candidates(), datetime.now(timezone.utc))
            if job:
                run_job(job, enrich)
                continue
        except Exception as e:                                   # never let the thread die
            log.error("Strava loop error: %s", e, exc_info=True)
        time.sleep(POLL_SECONDS)
