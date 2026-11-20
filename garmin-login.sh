#!/usr/bin/env bash
# Admin fallback: Garmin Connect login from the server terminal (two-factor code
# included). Normally not needed: people connect Garmin themselves in the app
# (Settings → Garmin Connect), code and all.
#
#   bash garmin-login.sh <Quarc username>
#
# Uses the Garmin email + password saved in the app, asks for the code Garmin
# sends and keeps Garmin's tokens in ./garth_tokens/<user id>/.
set -e

if [ -z "$1" ]; then
  echo "Usage: bash garmin-login.sh <Quarc username>"
  exit 1
fi

docker compose run --rm --no-deps sync python garmin_login.py "$1"
