#!/usr/bin/env bash
# ============================================================
#  Quarc Sport — Data Restore
#  Run on the NEW server from the Quarc_Sport directory:
#    bash restore.sh ./backup_20240101_120000
#
#  Coming from the old Quarc Garmin app instead? Use migrate-from-garmin.sh.
# ============================================================
set -e

BACKUP_DIR="$1"

if [ -z "$BACKUP_DIR" ] || [ ! -d "$BACKUP_DIR" ]; then
  echo "Usage: bash restore.sh <backup-dir>"
  echo "  e.g. bash restore.sh ./backup_20240101_120000"
  exit 1
fi

if [ -f "$BACKUP_DIR/garmin.sql" ] && [ ! -f "$BACKUP_DIR/sport.sql" ]; then
  echo "This is a backup of the old Quarc Garmin app. Use:  bash migrate-from-garmin.sh $BACKUP_DIR"
  exit 1
fi

if [ ! -f "$BACKUP_DIR/sport.sql" ] || [ ! -f "$BACKUP_DIR/.env" ]; then
  echo "ERROR: backup directory must contain sport.sql and .env"
  exit 1
fi

echo "Restoring from $BACKUP_DIR ..."

# 1. Restore .env (keeps JWT_SECRET and CREDENTIALS_KEY: nobody is logged out and
#    every saved Garmin login can still be decrypted)
echo "  · Restoring .env ..."
cp "$BACKUP_DIR/.env" .env

set -a; source .env 2>/dev/null || true; set +a
POSTGRES_DB="${POSTGRES_DB:-sport}"
POSTGRES_USER="${POSTGRES_USER:-sport}"

# 2. Restore garth tokens
if [ -d "$BACKUP_DIR/garth_tokens" ]; then
  echo "  · Restoring garth_tokens ..."
  cp -r "$BACKUP_DIR/garth_tokens" ./garth_tokens
fi

# 3. Start postgres only (to receive the restore)
echo "  · Starting database container ..."
docker compose up -d db
echo "  · Waiting for Postgres to be ready ..."
sleep 10

DB_CONTAINER=$(docker ps -q --filter "label=com.docker.compose.service=db" | head -1)
until docker exec "$DB_CONTAINER" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" &>/dev/null; do
  sleep 2
done

# 4. Restore the dump
echo "  · Restoring PostgreSQL database ($POSTGRES_DB) ..."
docker exec -i "$DB_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$BACKUP_DIR/sport.sql"

# 5. Start everything
echo "  · Starting all services ..."
docker compose up -d

echo ""
echo "Restore complete."
echo "Check the sync container is running: docker compose logs -f sync"
