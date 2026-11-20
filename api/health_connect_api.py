"""
Health Connect: the Android phone as a source ('health_connect').

Health Connect has no cloud API — the data can only be read on the phone. So the Android app reads
it (mobile/.../HealthConnectPlugin.kt) and uploads it here, where it is stored as its own source,
never mixed with Garmin or Strava. Only what the phone reports is stored: no scores are calculated.
"""

import hashlib
import re
from datetime import date, datetime, timedelta
from typing import Annotated, Optional

from fastapi import APIRouter, Depends
from psycopg2.extras import execute_values
from pydantic import BaseModel, Field, field_validator

import db
from auth import require_user

router = APIRouter(prefix="/api/health-connect")

SOURCE = "health_connect"
MAX_ROWS = 800          # per kind, per upload; the app sends bigger histories in several uploads

NonNegInt = Annotated[Optional[int], Field(default=None, ge=0, le=10_000_000)]
NonNegFloat = Annotated[Optional[float], Field(default=None, ge=0, le=10_000_000_000)]


def _sane_date(d: date) -> date:
    if d < date(2000, 1, 1) or d > date.today() + timedelta(days=2):
        raise ValueError("date out of range")
    return d


class DailyIn(BaseModel):
    date: date
    steps: NonNegInt
    distance_meters: NonNegFloat
    active_calories: NonNegInt
    total_calories: NonNegInt
    floors_ascended: NonNegInt
    resting_hr: NonNegInt
    min_hr_day: NonNegInt
    max_hr_day: NonNegInt

    _check = field_validator("date")(_sane_date)


class SleepIn(BaseModel):
    date: date
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_seconds: NonNegInt
    light_seconds: NonNegInt
    deep_seconds: NonNegInt
    rem_seconds: NonNegInt
    awake_seconds: NonNegInt

    _check = field_validator("date")(_sane_date)


class ActivityIn(BaseModel):
    record_id: str = Field(min_length=1, max_length=200)     # Health Connect's own id for the session
    start_time: datetime
    activity_type: str = Field(default="WORKOUT", max_length=50)
    name: Optional[str] = Field(default=None, max_length=200)
    duration_seconds: NonNegInt
    distance_meters: NonNegFloat
    calories: NonNegInt
    avg_hr: NonNegInt
    max_hr: NonNegInt
    elevation_gain_m: NonNegFloat


class Ingest(BaseModel):
    daily: list[DailyIn] = Field(default_factory=list, max_length=MAX_ROWS)
    sleep: list[SleepIn] = Field(default_factory=list, max_length=MAX_ROWS)
    activities: list[ActivityIn] = Field(default_factory=list, max_length=MAX_ROWS)
    device: Optional[str] = Field(default=None, max_length=100)


def activity_id_for(record_id: str) -> int:
    """Health Connect ids are strings; the table keys activities by a number. A stable 63-bit hash,
    so uploading the same session again updates it instead of adding a copy."""
    return int.from_bytes(hashlib.sha256(record_id.encode()).digest()[:8], "big") & (2**63 - 1)


def _type_key(raw: str) -> str:
    return re.sub(r"[^A-Z0-9_]", "_", raw.upper())[:50] or "WORKOUT"


def _upsert(cur, table: str, key_cols: list[str], cols: list[str], rows: list[tuple]) -> None:
    """Insert, or update the columns that now have a value (a column left out of an upload keeps its old value)."""
    if not rows:
        return
    all_cols = key_cols + cols
    updates = ", ".join(f"{c} = COALESCE(EXCLUDED.{c}, {table}.{c})" for c in cols)
    sql = (f"INSERT INTO {table} ({', '.join(all_cols)}) VALUES %s "
           f"ON CONFLICT (user_id, source, {key_cols[2]}) DO UPDATE SET {updates}")
    execute_values(cur, sql, rows)


@router.post("/ingest")
def ingest(body: Ingest, user: dict = Depends(require_user)):
    uid = user["id"]
    daily_cols = ["steps", "distance_meters", "active_calories", "total_calories", "floors_ascended",
                  "resting_hr", "min_hr_day", "max_hr_day"]
    sleep_cols = ["start_time", "end_time", "duration_seconds", "light_seconds", "deep_seconds", "rem_seconds", "awake_seconds"]
    act_cols = ["start_time", "activity_type", "name", "duration_seconds", "distance_meters", "calories",
                "avg_hr", "max_hr", "elevation_gain_m"]

    # one row per key within an upload (the last one wins), as Postgres refuses to update a row twice in one statement
    daily = {d.date: d for d in body.daily}
    sleep = {s.date: s for s in body.sleep}
    acts = {activity_id_for(a.record_id): a for a in body.activities}

    with db.cursor() as cur:
        _upsert(cur, "daily_summary", ["user_id", "source", "date"], daily_cols,
                [(uid, SOURCE, d.date, *[getattr(d, c) for c in daily_cols]) for d in daily.values()])
        _upsert(cur, "sleep", ["user_id", "source", "date"], sleep_cols,
                [(uid, SOURCE, s.date, *[getattr(s, c) for c in sleep_cols]) for s in sleep.values()])
        _upsert(cur, "activities", ["user_id", "source", "activity_id"], act_cols,
                [(uid, SOURCE, aid, a.start_time, _type_key(a.activity_type), a.name, a.duration_seconds,
                  a.distance_meters, a.calories, a.avg_hr, a.max_hr, a.elevation_gain_m) for aid, a in acts.items()])
        cur.execute(
            """
            INSERT INTO source_connections (user_id, source, status, display_name, last_sync_at)
            VALUES (%s, %s, 'connected', %s, now())
            ON CONFLICT (user_id, source) DO UPDATE SET
                status = 'connected', error = NULL, last_sync_at = now(),
                display_name = COALESCE(EXCLUDED.display_name, source_connections.display_name)
            """,
            (uid, SOURCE, body.device),
        )
    return {"daily": len(daily), "sleep": len(sleep), "activities": len(acts)}


@router.get("/status")
def status(user: dict = Depends(require_user)):
    with db.cursor() as cur:
        cur.execute("SELECT status, last_sync_at, display_name FROM source_connections WHERE user_id = %s AND source = %s",
                    (user["id"], SOURCE))
        row = cur.fetchone()
        counts = {}
        for table, name in (("daily_summary", "daily"), ("sleep", "sleep"), ("activities", "activities")):
            cur.execute(f"SELECT count(*) AS n FROM {table} WHERE user_id = %s AND source = %s", (user["id"], SOURCE))
            counts[name] = cur.fetchone()["n"]
    return {
        "connected": row is not None,
        "last_sync_at": row["last_sync_at"].isoformat() if row and row["last_sync_at"] else None,
        "device": row["display_name"] if row else None,
        "counts": counts,
    }


@router.delete("", status_code=204)
def disconnect(user: dict = Depends(require_user)):
    """Forget the phone as a source and delete what it uploaded. (Nothing is read from the phone any more:
    the app also stops; what Health Connect holds on the phone is untouched.)"""
    with db.cursor() as cur:
        for table in ("tours", "activities", "daily_summary", "sleep", "hrv"):   # tours first: their links cascade
            cur.execute(f"DELETE FROM {table} WHERE user_id = %s AND source = %s", (user["id"], SOURCE))
        cur.execute("DELETE FROM source_connections WHERE user_id = %s AND source = %s", (user["id"], SOURCE))
