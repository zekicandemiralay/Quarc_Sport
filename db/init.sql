-- ─────────────────────────────────────────────────────────────────────────────
-- Quarc Sport — database schema
--
-- Applied automatically the first time the Postgres volume is created (see
-- docker-compose.yml). Every statement is idempotent, and the sync service
-- re-applies the same file on start-up (sync/schema.py), so adding a column
-- here is all a future schema change needs.
--
-- Accounts live in quarc-auth (Quarc_Store/auth), not here. `users` only holds
-- what Quarc Sport itself needs per person: the id quarc-auth gave them, and
-- their (encrypted) Garmin Connect credentials. Ids are quarc-auth UUIDs.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    id               TEXT PRIMARY KEY,
    username         TEXT NOT NULL,
    garmin_email_enc BYTEA,
    garmin_pass_enc  BYTEA,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

-- Garmin connection and import progress, per person. The API writes the status
-- when someone connects in the app; the sync service keeps it current.
--   garmin_status: NULL (not connected) | connected | needs_code (Garmin wants a
--   two-factor code again) | auth_failed (login no longer accepted) | error (temporary)
ALTER TABLE users ADD COLUMN IF NOT EXISTS garmin_status     TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS garmin_error      TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS sync_requested_at TIMESTAMPTZ;  -- "sync me now": just connected, or Sync now
ALTER TABLE users ADD COLUMN IF NOT EXISTS syncing_since     TIMESTAMPTZ;  -- set while the sync service works on them
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_sync_at      TIMESTAMPTZ;  -- last finished sync of the recent days
ALTER TABLE users ADD COLUMN IF NOT EXISTS retry_after       TIMESTAMPTZ;  -- backing off after a Garmin error
-- Import cursor: every day from history_from to synced_until has been fetched.
-- The newest month comes first, then older history going backwards to history_target.
ALTER TABLE users ADD COLUMN IF NOT EXISTS synced_until      DATE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS history_from      DATE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS history_target    DATE;

-- Logins saved before garmin_status existed.
UPDATE users SET garmin_status = 'connected'
WHERE garmin_status IS NULL AND garmin_email_enc IS NOT NULL;

-- Daily summary: steps, calories, stress, body battery, SpO2, hydration
CREATE TABLE IF NOT EXISTS daily_summary (
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    source              TEXT NOT NULL DEFAULT 'garmin',
    date                DATE NOT NULL,
    steps               INT,
    step_goal           INT,
    distance_meters     FLOAT,
    active_calories     INT,
    total_calories      INT,
    floors_ascended     INT,
    floors_descended    INT,
    active_time_seconds INT,
    sedentary_seconds   INT,
    stress_avg          INT,
    stress_rest         INT,
    body_battery_high   INT,
    body_battery_low    INT,
    spo2_avg            FLOAT,
    spo2_min            FLOAT,
    hydration_ml        INT,
    resting_hr          INT,
    min_hr_day          INT,
    max_hr_day          INT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, source, date)
);

-- Sleep: duration, stages, sleep score
CREATE TABLE IF NOT EXISTS sleep (
    user_id          TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    source              TEXT NOT NULL DEFAULT 'garmin',
    date             DATE NOT NULL,
    start_time       TIMESTAMPTZ,
    end_time         TIMESTAMPTZ,
    duration_seconds INT,
    light_seconds    INT,
    deep_seconds     INT,
    rem_seconds      INT,
    awake_seconds    INT,
    sleep_score      INT,
    avg_spo2         FLOAT,
    avg_respiration  FLOAT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, source, date)
);

-- HRV
CREATE TABLE IF NOT EXISTS hrv (
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    source              TEXT NOT NULL DEFAULT 'garmin',
    date                DATE NOT NULL,
    hrv_weekly_avg      INT,
    hrv_last_night      INT,
    hrv_last_night_5min INT,
    hrv_status          VARCHAR(20),
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, source, date)
);

-- Activities / workouts
CREATE TABLE IF NOT EXISTS activities (
    user_id              TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    source              TEXT NOT NULL DEFAULT 'garmin',
    activity_id          BIGINT NOT NULL,
    start_time           TIMESTAMPTZ,
    activity_type        VARCHAR(50),
    name                 VARCHAR(200),
    duration_seconds     INT,
    distance_meters      FLOAT,
    avg_hr               INT,
    max_hr               INT,
    calories             INT,
    avg_pace_sec_per_km  FLOAT,
    aerobic_te           FLOAT,
    anaerobic_te         FLOAT,
    start_lat            DOUBLE PRECISION,
    start_lng            DOUBLE PRECISION,
    end_lat              DOUBLE PRECISION,
    end_lng              DOUBLE PRECISION,
    elevation_gain_m     FLOAT,
    avg_speed_mps        FLOAT,
    avg_cadence          INTEGER,
    avg_power            INTEGER,
    polyline             JSONB,
    country              VARCHAR(10),
    weather_data         JSONB,
    country_crossings    JSONB,
    weather_grid_fetched BOOLEAN DEFAULT FALSE,
    radar_timestamps     BIGINT[],
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, source, activity_id)
);

-- Tours (named trip groups)
CREATE TABLE IF NOT EXISTS tours (
    id          SERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    source      TEXT NOT NULL DEFAULT 'garmin',
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tour_activities (
    tour_id     INT REFERENCES tours(id) ON DELETE CASCADE,
    activity_id BIGINT,
    PRIMARY KEY (tour_id, activity_id)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Sources. Every data row says where it came from: 'garmin' (Garmin Connect),
-- 'strava', 'health_connect' (the Android phone). Sources are never merged — the
-- app shows one at a time, and the person picks.
-- Databases created before this: every existing row is Garmin's, and the keys now
-- include the source so two sources can hold the same day.
-- ─────────────────────────────────────────────────────────────────────────────
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['daily_summary', 'sleep', 'hrv', 'activities'] LOOP
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = t AND column_name = 'source') THEN
      EXECUTE format('ALTER TABLE %I ADD COLUMN source TEXT NOT NULL DEFAULT %L', t, 'garmin');
      EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', t, t || '_pkey');
      EXECUTE format('ALTER TABLE %I ADD PRIMARY KEY (%s)', t,
                     CASE WHEN t = 'activities' THEN 'user_id, source, activity_id' ELSE 'user_id, source, date' END);
    END IF;
  END LOOP;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema = 'public' AND table_name = 'tours' AND column_name = 'source') THEN
    ALTER TABLE tours ADD COLUMN source TEXT NOT NULL DEFAULT 'garmin';
  END IF;
END $$;

-- Connections to sources other than Garmin (Garmin keeps its columns on `users`).
--   strava:         credentials_enc = Fernet-encrypted JSON {access_token, refresh_token, expires_at}
--   health_connect: no credentials; the Android app uploads, last_sync_at = last upload
--   status: connected | auth_failed (access revoked / login no longer accepted) | error (temporary)
CREATE TABLE IF NOT EXISTS source_connections (
    user_id           TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    source            TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'connected',
    error             TEXT,
    credentials_enc   BYTEA,
    external_id       TEXT,                 -- Strava athlete id
    display_name      TEXT,                 -- Strava athlete name
    sync_requested_at TIMESTAMPTZ,          -- "sync me now"
    syncing_since     TIMESTAMPTZ,          -- set while the sync service works on them
    last_sync_at      TIMESTAMPTZ,
    retry_after       TIMESTAMPTZ,          -- backing off after an error / rate limit
    newest_at         TIMESTAMPTZ,          -- Strava: start of the newest activity stored (incremental sync)
    history_before    TIMESTAMPTZ,          -- Strava: the first import has covered everything newer than this
    history_done      BOOLEAN NOT NULL DEFAULT FALSE,
    items_synced      INT NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, source)
);

-- ERA5 weather grid — shared across users, keyed by rounded lat/lng + date + hour
CREATE TABLE IF NOT EXISTS weather_grid_points (
    lat                FLOAT    NOT NULL,
    lng                FLOAT    NOT NULL,
    date               DATE     NOT NULL,
    hour               SMALLINT NOT NULL,
    temperature_2m     FLOAT,
    precipitation      FLOAT,
    wind_speed_10m     FLOAT,
    wind_direction_10m FLOAT,
    fetched_at         TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (lat, lng, date, hour)
);

-- RainViewer radar tiles — shared, stored permanently by unix timestamp + tile coords
CREATE TABLE IF NOT EXISTS weather_radar_tiles (
    timestamp_unix BIGINT   NOT NULL,
    z              SMALLINT NOT NULL,
    x              INTEGER  NOT NULL,
    y              INTEGER  NOT NULL,
    tile_data      BYTEA    NOT NULL,
    fetched_at     TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (timestamp_unix, z, x, y)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_activities_user_start ON activities(user_id, source, start_time);
CREATE INDEX IF NOT EXISTS idx_daily_user_date       ON daily_summary(user_id, date);
CREATE INDEX IF NOT EXISTS idx_sleep_user_date       ON sleep(user_id, date);
CREATE INDEX IF NOT EXISTS idx_tours_user            ON tours(user_id);
CREATE INDEX IF NOT EXISTS idx_grid_points_area      ON weather_grid_points(date, lat, lng);
