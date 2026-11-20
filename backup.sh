#!/usr/bin/env bash
# ============================================================
#  Quarc Sport — Data Backup
#  Run on the OLD server from the project directory:
#    bash backup.sh
#
#  Creates: ./backup_YYYYMMDD_HHMMSS/
#    sport.sql          — full PostgreSQL dump (all synced data, tours, saved Garmin logins)
#    garth_tokens/      — Garmin Connect auth tokens (two-factor accounts)
#    .env               — secrets and config (JWT_SECRET, CREDENTIALS_KEY, ...)
#
#  Keep the folder private: .env and the dump together can decrypt every saved Garmin login.
# ============================================================
set -e

BACKUP_DIR="./backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

echo "Backing up to $BACKUP_DIR ..."

if [ -f .env ]; then
  set -a; source .env 2>/dev/null || true; set +a
fi

POSTGRES_DB="${POSTGRES_DB:-sport}"
POSTGRES_USER="${POSTGRES_USER:-sport}"

DB_CONTAINER=$(docker ps -q --filter "label=com.docker.compose.service=db" | head -1)
if [ -z "$DB_CONTAINER" ]; then
  echo "ERROR: db container not running. Start it first: docker compose up -d db"
  exit 1
fi

echo "  · Dumping PostgreSQL database ($POSTGRES_DB) ..."
docker exec "$DB_CONTAINER" pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > "$BACKUP_DIR/sport.sql"

echo "  · Copying garth_tokens ..."
if [ -d "./garth_tokens" ] && [ "$(ls -A ./garth_tokens 2>/dev/null)" ]; then
  cp -r ./garth_tokens "$BACKUP_DIR/garth_tokens"
else
  echo "    (no garth_tokens found — skipping)"
fi

echo "  · Copying .env ..."
cp .env "$BACKUP_DIR/.env"

SQL_SIZE=$(du -sh "$BACKUP_DIR/sport.sql" | awk '{print $1}')
echo ""
echo "Backup complete: $BACKUP_DIR"
echo "  sport.sql : $SQL_SIZE"
echo ""
echo "Next: copy this folder to the new server."
echo "  scp -r $BACKUP_DIR user@new-server:~/Quarc_Sport/"
