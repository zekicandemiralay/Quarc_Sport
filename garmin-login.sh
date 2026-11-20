#!/usr/bin/env bash
# One-time Garmin Connect login for accounts with two-factor authentication.
#
#   bash garmin-login.sh <Quarc username>
#
# First save the person's Garmin email + password in the app (Settings → Garmin
# Connect). This then asks for the MFA code Garmin sends and keeps Garmin's OAuth
# tokens in ./garth_tokens/<user id>/, so the hourly sync no longer needs a code.
set -e

if [ -z "$1" ]; then
  echo "Usage: bash garmin-login.sh <Quarc username>"
  exit 1
fi

docker compose run --rm --no-deps sync python garmin_login.py "$1"
