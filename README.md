# Quarc Sport

Your sport and health data on your own server, for the Quarc suite. It pulls your whole Garmin Connect history
into a database and shows it as charts and maps: daily stats, sleep, HRV, every activity with its route, personal
bests, and multi-day tours with weather. It uses the **same login as every other Quarc app** and runs on the web,
Android, Windows/macOS/Linux and iOS (as a PWA).

It used to be **Quarc Garmin**; this is the same app moved onto the Quarc suite's conventions (shared login, HTTPS port
4007, Android/desktop apps, English + Türkçe). Garmin Connect is still where the data comes from.

**What you get**
- Overview, Health, Sleep, Activity and Touring tabs; 7 days to all-time ranges
- Daily stats: steps, calories, stress, body battery, SpO2, resting HR, floors, active time
- Sleep stages and score, sleep debt, schedule consistency, respiration; HRV trends
- All activities with GPS maps (routes coloured by speed), pace trend, personal bests, countries visited
- Touring: group activities into named multi-day tours with weather overlay (ERA5 + radar), border crossings and sleep stops on the map
- Several people on one server: each sees only their own data and connects their own Garmin account
- English and Türkçe

---

## Part 1 — Server setup

### Requirements
- Docker and Docker Compose
- A machine that stays on, running [Tailscale](https://tailscale.com) with HTTPS certificates enabled
- The shared `quarc-auth` login service running (see `Quarc_Store/auth/README.md`)

### Install
```bash
docker network create quarcnet-shared      # once per server, if not already done
git clone https://github.com/zekicandemiralay/Quarc_Sport.git
cd Quarc_Sport
cp .env.example .env
```
Edit `.env`:

| Variable | Description |
|---|---|
| `JWT_SECRET` | Must equal `Quarc_Store/auth/.env`'s value exactly (that's what makes one login work everywhere) |
| `SECURE_COOKIE` | Must match quarc-auth (default `true`) |
| `CREDENTIALS_KEY` | Encrypts everyone's saved Garmin login. Generate once (command in `.env.example`) and **keep it** — it is part of every backup |
| `POSTGRES_PASSWORD` | Any password for the local database |

```bash
bash deploy.sh
bash check.sh
```
Open `https://quarcnet0.tail84500c.ts.net:4007` (Music 4000, Notes 4001, Weather 4002, Translate 4003,
Calendar 4005, Store 4006, **Sport 4007**) and sign in with your Quarc account.

### Connecting Garmin
Everyone does this themselves, no server access needed: **Settings → Garmin Connect**, enter the Garmin email and
password, **Connect Garmin**.

- The login is checked with Garmin right away: a wrong email or password (or an account that doesn't exist) is said so on
  the spot and nothing is saved. Repeated tries are limited, to spare the Garmin account a lockout.
- **Two-factor accounts:** Garmin sends its code (email or phone) and the app shows a box to type it in.
- Garmin's sign-in tokens are kept in `garth_tokens/` (shared by `api` and `sync`), so syncing never needs a code again.
  If Garmin ever asks for a new one, or stops accepting a changed password, the app shows it (Settings and a strip on the
  dashboard) and the person fixes it there: **Send me a new code** / re-enter the login.
- The import starts within seconds: **the latest month first** (the dashboard fills in by itself), then older history
  newest-first, back up to `SYNC_BACKFILL_DAYS` (10 years), with progress shown in the app. After that it syncs every hour,
  and **Sync now** asks for one right away.
- Credentials are stored encrypted and never returned by the API (Settings only shows a masked email).

`bash garmin-login.sh <Quarc username>` still exists as an admin fallback (a terminal sign-in with the code), but isn't
needed normally.

### Moving from the old Quarc Garmin app
On the old server run `bash backup.sh` in `Quarc_Garmin`, copy the backup folder here, then:
```bash
cp .env.example .env     # set JWT_SECRET and POSTGRES_PASSWORD (leave CREDENTIALS_KEY empty)
bash migrate-from-garmin.sh ./backup_YYYYMMDD_HHMMSS
```
People are matched **by username** to their Quarc accounts, so everyone needs one (sign up in any Quarc app). A person
without one is skipped and listed; create the account and run the script again (it is safe to repeat). Their data, tours,
saved Garmin login and Garmin tokens come across; the old app's `SECRET_KEY` becomes `CREDENTIALS_KEY` so the saved
logins keep working.

### Updating, backup, health
```bash
bash deploy.sh                 # git pull + rebuild (or: deploy.sh frontend|api|sync)
bash backup.sh                 # → ./backup_YYYYMMDD_HHMMSS/  (sport.sql + garth_tokens + .env)
bash restore.sh <backup-dir>   # on a new server
bash check.sh                  # containers, HTTPS, login, database, sync
docker compose logs -f sync    # what the Garmin sync is doing
```
The backup holds `.env` and the database together, which can decrypt every saved Garmin login — keep it private.

Map tiles come from OpenStreetMap: the `frontend` container fetches them once, identifies itself to OSM as Quarc Sport, and
keeps them in a cache volume (`tile_cache`), so no map traffic leaves your devices and your login cookie never goes upstream.

Services: `db` (PostgreSQL, internal only), `sync` (Garmin → database, weather), `api` (FastAPI, port 8000 internal),
`frontend` (nginx, HTTPS on `4007`, sends `/api/auth/*` to quarc-auth and `/api/*` to the API).

---

## Part 2 — User setup

Everyone needs Tailscale connected, then:
- **Android**: install the APK from [Releases](https://github.com/zekicandemiralay/Quarc_Sport/releases/latest) (or from Quarc Store, which also signs you in)
- **Windows / macOS / Linux**: install the desktop app from Releases (see `desktop/BUILD.md`)
- **iOS**: open the URL in Safari → Share → **Add to Home Screen**
- **Anyone else**: open the URL in a browser

Sign in with the same username and password as the other Quarc apps.

---

## API

Everything is under `/api/`; login is the cookie set by quarc-auth. The API port is not published; the routes are in `api/main.py`.

| Method | Endpoint | |
|---|---|---|
| GET | `/api/health` | Public: API + database |
| GET | `/api/me` | Who you are here, and whether Garmin is connected |
| PUT / DELETE | `/api/garmin/credentials` | Save (write-only) / forget your Garmin login |
| GET | `/api/range` `/api/daily` `/api/sleep` `/api/hrv` | Your health data (`start`, `end` = `YYYY-MM-DD`) |
| GET | `/api/activities` `/api/activities/map` `/api/activities/countries` | Your workouts |
| GET | `/api/summary` `/api/touring` | Aggregates, touring view |
| GET / POST / PUT / DELETE | `/api/tours`, `/api/tours/{id}` | Named tours |
| GET | `/api/weather-grid`, `/api/activities/{id}/weather-grid`, `/api/activities/{id}/radar-timestamps`, `/api/radar/...` | Weather layers |

---

## Development

```
api/        FastAPI; verifies quarc-auth's login cookie, stores Garmin credentials encrypted
sync/       Garmin Connect → PostgreSQL (hourly), ERA5 + radar weather, one-time Garmin/legacy helpers
db/         init.sql — the whole schema, applied (idempotently) by api and sync on start-up
frontend/   React 18 + TypeScript + Vite + Tailwind + Recharts + Leaflet + zustand + i18next
desktop/    Tauri 2 wrapper (loads the server URL)
mobile/     Capacitor 6 Android wrapper (bundles the web build)
.github/    Release workflow: Windows/macOS/Linux installers + signed Android APK on `v*` tags
```

```bash
# a throwaway database for the API tests (they need real PostgreSQL)
docker run -d --name sport-test-pg -e POSTGRES_USER=sport -e POSTGRES_PASSWORD=test -e POSTGRES_DB=sport -p 55432:5432 postgres:16
cd api && pip install -r requirements.txt pytest httpx && python -m pytest tests

# frontend — proxies /api/auth → :3902 (quarc-auth) and /api → :8000
cd frontend && npm install && npm run dev
cd frontend && npm test      # translations: en/tr keys match, every t('key') exists
```

### Releasing
Tag `vX.Y.Z` and push; the workflow builds the desktop installers and a signed Android APK. It uses two repository
secrets, both set: `KEYSTORE_BASE64` (base64 of this app's own keystore, key alias `quarc-sport`) and `KEYSTORE_PASSWORD`.
The keystore and its password live only in `mobile/quarc-sport.keystore` and `mobile/keystore-password.txt` (git-ignored);
every release must be signed with it, or Android refuses the update.
Keep patch bumps within the Quarc Store promo window (`'com.quarc.sport': '1.0.10'` in `frontend/src/lib/storeSso.js`).

### Quarc Store
The app carries the Store kit (single sign-on, phone sign-in, required updates). It is in the Store's catalog
(`Quarc_Store/backend/src/seed.js`) and in `STORE_PROMO_UNTIL` in `Quarc_Store/sso-kit/web/storeSso.js`, so the Store
installs it, updates it and signs it in like the other Quarc apps.

### Logo
Master: `QuarcNet/logos/sport.svg` (+ `logos/adaptive/sport-*.svg`). `frontend/public/logo.png` (512 px), `desktop/logo.png` and
`mobile/resources/icon.png` (1024 px) are exports of it. To regenerate the platform icons:
```bash
cd desktop && npx tauri icon logo.png
cd ../mobile && npm run icons
```
