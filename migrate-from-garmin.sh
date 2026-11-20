#!/usr/bin/env bash
# ============================================================
#  Quarc Sport — import the data of the old Quarc Garmin app
#
#  1. On the OLD Quarc Garmin server:   bash backup.sh      (creates ./backup_YYYYMMDD_HHMMSS)
#  2. Copy that folder here, then, from the Quarc_Sport directory:
#       cp .env.example .env     # set JWT_SECRET (same as the other Quarc apps) + POSTGRES_PASSWORD
#       bash migrate-from-garmin.sh ./backup_YYYYMMDD_HHMMSS
#
#  People are matched by username against the shared Quarc accounts (quarc-auth must be running).
#  Their activities, sleep, HRV, tours, saved Garmin login and Garmin tokens come along. Someone
#  without a Quarc account yet is skipped and listed — they sign up, then run this again (safe to repeat).
# ============================================================
set -e

OLD="$1"
if [ -z "$OLD" ] || [ ! -f "$OLD/garmin.sql" ] || [ ! -f "$OLD/.env" ]; then
  echo "Usage: bash migrate-from-garmin.sh <backup-dir of the old Quarc Garmin>"
  echo "  (the folder must contain garmin.sql and .env — made by the old app's backup.sh)"
  exit 1
fi
if [ ! -f .env ]; then
  echo "No .env yet. Run:  cp .env.example .env   then set JWT_SECRET and POSTGRES_PASSWORD, and run this again."
  exit 1
fi
docker ps -q --filter "name=quarc-auth" | grep -q . || { echo "quarc-auth is not running (see Quarc_Store/auth/README.md)."; exit 1; }

envval() { grep -E "^$2=" "$1" | head -1 | cut -d= -f2- | tr -d '\r'; }

# The old app encrypted saved Garmin logins with its SECRET_KEY. Carrying it over as CREDENTIALS_KEY
# is what keeps those logins usable.
OLD_KEY=$(envval "$OLD/.env" SECRET_KEY)
OLD_USER=$(envval "$OLD/.env" POSTGRES_USER); OLD_USER=${OLD_USER:-garmin}
NEW_KEY=$(envval .env CREDENTIALS_KEY)
if [ -z "$NEW_KEY" ]; then
  [ -n "$OLD_KEY" ] || { echo "The old .env has no SECRET_KEY — cannot carry the saved Garmin logins over."; exit 1; }
  if grep -q '^CREDENTIALS_KEY=' .env; then
    sed -i "s|^CREDENTIALS_KEY=.*|CREDENTIALS_KEY=$OLD_KEY|" .env
  else
    printf '\nCREDENTIALS_KEY=%s\n' "$OLD_KEY" >> .env
  fi
  echo "  · CREDENTIALS_KEY set from the old SECRET_KEY"
elif [ "$NEW_KEY" != "$OLD_KEY" ]; then
  echo "  ⚠ CREDENTIALS_KEY differs from the old SECRET_KEY: saved Garmin logins will be copied but can't be"
  echo "    decrypted, so people have to re-enter them in Settings. (Continue anyway with Ctrl-C to abort.)"
  sleep 5
fi

set -a; source .env 2>/dev/null || true; set +a
POSTGRES_DB="${POSTGRES_DB:-sport}"
POSTGRES_USER="${POSTGRES_USER:-sport}"

echo "  · Starting the database ..."
docker compose up -d db
DB=$(docker compose ps -q db)
until docker exec "$DB" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" &>/dev/null; do sleep 2; done

echo "  · Loading the old dump into a scratch database ..."
docker exec "$DB" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q \
  -c "DROP DATABASE IF EXISTS legacy" -c "CREATE DATABASE legacy"
# The dump names its old owner role; create it so the restore doesn't trip over ownership.
if [ "$OLD_USER" != "$POSTGRES_USER" ]; then
  docker exec "$DB" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q \
    -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$OLD_USER') THEN CREATE ROLE \"$OLD_USER\"; END IF; END \$\$"
fi
docker exec -i "$DB" psql -U "$POSTGRES_USER" -d legacy -q -v ON_ERROR_STOP=1 < "$OLD/garmin.sql" > /dev/null

echo "  · Reading the Quarc accounts ..."
AUTH_USERS=$(docker exec quarc-auth node -e "const D=require('better-sqlite3');const d=new D(process.env.DB_PATH||'/app/data/auth.db',{readonly:true});console.log(JSON.stringify(d.prepare('SELECT id, username FROM users').all()))")

echo "  · Importing ..."
MOUNT=()
if [ -d "$OLD/garth_tokens" ]; then MOUNT=(-v "$(cd "$OLD" && pwd)/garth_tokens:/legacy_tokens:ro" -e LEGACY_TOKENS=/legacy_tokens); fi
docker compose run --rm --no-deps -T -e LEGACY_DB=legacy -e "AUTH_USERS=$AUTH_USERS" "${MOUNT[@]}" sync python migrate_legacy.py

docker exec "$DB" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q -c "DROP DATABASE IF EXISTS legacy"

echo "  · Starting all services ..."
docker compose up -d --build

echo ""
echo "Migration finished. Check with:  bash check.sh   and   docker compose logs -f sync"
