"""
One-time import of data from the old Quarc Garmin app (see ../migrate-from-garmin.sh).

The old app had its own accounts with numeric ids; Quarc Sport uses the shared quarc-auth
accounts (UUID ids). People are matched by USERNAME: a legacy user whose name matches a
quarc-auth account gets all of their data, saved Garmin login and tours copied across under
the new id. A legacy user with no matching account is skipped and reported — create the
account (sign up in any Quarc app), then run the migration again. Re-running is safe: rows
that already exist are left alone.

Runs inside the sync container, which already has psycopg2 and the schema.
  LEGACY_DB      name of the database the old dump was restored into
  AUTH_USERS     JSON [{"id": "...", "username": "..."}] from quarc-auth
  LEGACY_TOKENS  (optional) the old garth_tokens folder, mounted read-only
"""

import json
import os
import shutil
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

import db

JSONB_COLUMNS = {"polyline", "weather_data", "country_crossings"}
BATCH = 500


def connect(dbname: str):
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "db"),
        port=os.getenv("POSTGRES_PORT", 5432),
        dbname=dbname,
        user=os.getenv("POSTGRES_USER", "sport"),
        password=os.getenv("POSTGRES_PASSWORD"),
    )


def columns(conn, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
            (table,),
        )
        return [r[0] for r in cur.fetchall()]


def copy_table(old, new, table: str, user_map: dict | None, conflict: str) -> int:
    """Copy `table` from the legacy DB; rows of users without a mapping are dropped."""
    legacy_cols = set(columns(old, table))
    cols = [c for c in columns(new, table) if c in legacy_cols]
    if user_map is not None and "user_id" not in cols:
        raise RuntimeError(f"{table} has no user_id column")

    select = ", ".join(f"{c}::text" if c in JSONB_COLUMNS else c for c in cols)
    template = "(" + ", ".join("%s::jsonb" if c in JSONB_COLUMNS else "%s" for c in cols) + ")"
    insert = f"INSERT INTO {table} ({', '.join(cols)}) VALUES %s ON CONFLICT {conflict} DO NOTHING"
    uidx = cols.index("user_id") if user_map is not None else None

    copied = 0
    with old.cursor(name=f"copy_{table}") as src:  # server-side cursor: tables can be large
        src.itersize = BATCH
        src.execute(f"SELECT {select} FROM {table}")
        while True:
            rows = src.fetchmany(BATCH)
            if not rows:
                break
            out = []
            for r in rows:
                if uidx is not None:
                    nid = user_map.get(r[uidx])
                    if nid is None:
                        continue
                    r = r[:uidx] + (nid,) + r[uidx + 1:]
                out.append(r)
            if out:
                with new.cursor() as cur:
                    execute_values(cur, insert, out, template=template)
                new.commit()
                copied += len(out)
    return copied


def main() -> int:
    legacy_db = os.environ["LEGACY_DB"]
    auth = {u["username"].lower(): u["id"] for u in json.loads(os.environ["AUTH_USERS"])}

    db.ensure_schema()
    old, new = connect(legacy_db), db.get_conn()

    with old.cursor() as cur:
        cur.execute("SELECT id, username, garmin_email_enc, garmin_pass_enc, created_at FROM users ORDER BY id")
        legacy_users = cur.fetchall()

    user_map: dict[int, str] = {}
    skipped = []
    for oid, username, email_enc, pass_enc, created in legacy_users:
        nid = auth.get(username.lower())
        if nid is None:
            skipped.append(username)
            continue
        user_map[oid] = nid
        with new.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, username, garmin_email_enc, garmin_pass_enc, created_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    garmin_email_enc = COALESCE(users.garmin_email_enc, EXCLUDED.garmin_email_enc),
                    garmin_pass_enc  = COALESCE(users.garmin_pass_enc,  EXCLUDED.garmin_pass_enc)
                """,
                (nid, username, email_enc, pass_enc, created),
            )
        new.commit()
        print(f"  user {username}: legacy id {oid} -> {nid}")

    for username in skipped:
        print(f"  SKIPPED {username}: no Quarc account with that username "
              f"(sign up in any Quarc app, then run the migration again)")

    for table, conflict in (
        ("daily_summary", "(user_id, source, date)"),
        ("sleep", "(user_id, source, date)"),
        ("hrv", "(user_id, source, date)"),
        ("activities", "(user_id, source, activity_id)"),
    ):
        print(f"  {table}: {copy_table(old, new, table, user_map, conflict)} rows copied")

    # Tours get new serial ids, so they are matched on (user, name, created_at) to stay re-runnable.
    tour_map: dict[int, int] = {}
    with old.cursor() as src:
        src.execute("SELECT id, user_id, name, description, created_at, updated_at FROM tours ORDER BY id")
        legacy_tours = src.fetchall()
    for oid, ouser, name, description, created, updated in legacy_tours:
        nuser = user_map.get(ouser)
        if nuser is None:
            continue
        with new.cursor() as cur:
            cur.execute(
                "SELECT id FROM tours WHERE user_id = %s AND name = %s AND created_at = %s",
                (nuser, name, created),
            )
            row = cur.fetchone()
            if row:
                tour_map[oid] = row[0]
            else:
                cur.execute(
                    "INSERT INTO tours (user_id, name, description, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (nuser, name, description, created, updated),
                )
                tour_map[oid] = cur.fetchone()[0]
        new.commit()
    with old.cursor() as src:
        src.execute("SELECT tour_id, activity_id FROM tour_activities")
        links = [(tour_map[t], a) for t, a in src.fetchall() if t in tour_map]
    if links:
        with new.cursor() as cur:
            execute_values(
                cur, "INSERT INTO tour_activities (tour_id, activity_id) VALUES %s ON CONFLICT DO NOTHING", links
            )
        new.commit()
    print(f"  tours: {len(tour_map)} tours, {len(links)} activity links")

    # Shared weather caches (no user): radar tiles can't be re-downloaded once RainViewer drops them.
    print(f"  weather_grid_points: {copy_table(old, new, 'weather_grid_points', None, '(lat, lng, date, hour)')} rows copied")
    print(f"  weather_radar_tiles: {copy_table(old, new, 'weather_radar_tiles', None, '(timestamp_unix, z, x, y)')} rows copied")

    # Garmin OAuth tokens (two-factor accounts): the old layout kept user 1's files at the top
    # level of garth_tokens/ and every other user's in garth_tokens/<id>/.
    tokens = os.getenv("LEGACY_TOKENS")
    if tokens and Path(tokens).is_dir():
        for oid, nid in user_map.items():
            src_dir = Path(tokens) if oid == 1 else Path(tokens) / str(oid)
            files = [f for f in src_dir.glob("*") if f.is_file()] if src_dir.is_dir() else []
            dest = Path(db_token_dir(nid))
            if files and not any(dest.iterdir()):
                for f in files:
                    shutil.copy2(f, dest / f.name)
                print(f"  garmin tokens: legacy user {oid} -> {nid}")

    old.close()
    new.close()
    print("Done." + (f" {len(skipped)} legacy user(s) skipped — see above." if skipped else ""))
    return 0


def db_token_dir(user_id: str) -> str:
    root = Path(os.getenv("GARTH_TOKEN_ROOT", "/root/.garth")) / user_id
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


if __name__ == "__main__":
    sys.exit(main())
