#!/usr/bin/env bash
# Usage:
#   bash deploy.sh            — rebuild frontend, api and sync
#   bash deploy.sh frontend   — rebuild frontend only
#   bash deploy.sh api        — rebuild api only
#   bash deploy.sh sync       — rebuild the Garmin sync service only
#
# Uses --no-deps so a rebuild never restarts the database (or anything else
# that is still happily running).

set -e

TARGET=${1:-all}

pull_latest() {
  echo "Pulling latest code..."
  git pull
}

build() {
  local svc=$1
  echo "Building $svc..."
  docker compose up -d --build --no-deps "$svc"
  echo "$svc updated."
}

pull_latest

# The database must be up before api/sync can start (first deploy, or after a reboot).
docker compose up -d --no-deps db

case "$TARGET" in
  frontend) build frontend ;;
  api)      build api ;;
  sync)     build sync ;;
  all)      build api; build sync; build frontend ;;
  *)        echo "Usage: bash deploy.sh [frontend|api|sync|all]"; exit 1 ;;
esac

echo "Done."
