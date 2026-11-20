#!/usr/bin/env bash
# ============================================================
#  Quarc Sport — Health check. Run from the project root:
#    bash check.sh
# ============================================================
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
FAIL=0
ok()   { printf "  ${GREEN}✓${NC} %s\n" "$1"; }
warn() { printf "  ${YELLOW}⚠${NC} %s\n" "$1"; }
fail() { printf "  ${RED}✗${NC} %s\n" "$1"; FAIL=$((FAIL+1)); }

[ -f .env ] && { set -a; source .env 2>/dev/null; set +a; } || warn "No .env file found"
HTTPS_PORT=${HTTPS_PORT:-4007}
BASE="https://localhost:${HTTPS_PORT}"
POSTGRES_DB=${POSTGRES_DB:-sport}
POSTGRES_USER=${POSTGRES_USER:-sport}

printf "\n${BOLD}Quarc Sport — diagnostic${NC}  $(date '+%Y-%m-%d %H:%M:%S')\n\n"
docker info &>/dev/null || { printf "  ${RED}✗ Docker not accessible (try sudo)${NC}\n"; exit 1; }

for svc in db sync api frontend; do
  CID=$(docker ps -q --filter "label=com.docker.compose.service=${svc}" | head -1)
  [ -n "$CID" ] && ok "$svc: running" || fail "$svc: not running"
done
docker ps -q --filter "name=quarc-auth" | grep -q . && ok "quarc-auth: running" || fail "quarc-auth: not running (see Quarc_Store/auth/README.md)"

CODE=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 5 "${BASE}/" || echo 0)
[ "$CODE" = "200" ] && ok "HTTPS :${HTTPS_PORT} serves the app" || fail "HTTPS :${HTTPS_PORT} returned ${CODE}"

CODE=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 5 "${BASE}/api/health" || echo 0)
[ "$CODE" = "200" ] && ok "/api/health reachable through nginx (API + database)" || fail "/api/health returned ${CODE}"

CODE=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 5 "${BASE}/api/me" || echo 0)
[ "$CODE" = "401" ] && ok "data endpoints require login (/api/me → 401 when anonymous)" || fail "/api/me anonymous returned ${CODE} (expected 401)"

CODE=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 5 "${BASE}/api/auth/me" || echo 0)
[ "$CODE" = "401" ] && ok "shared login reachable (/api/auth/me → 401 when anonymous)" || fail "/api/auth/me returned ${CODE} (expected 401)"

# What the sync service has gathered
q() { docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }
USERS=$(q "SELECT count(*) FROM users")
WITH_GARMIN=$(q "SELECT count(*) FROM users WHERE garmin_email_enc IS NOT NULL")
if [ -n "$USERS" ]; then
  ok "database: ${USERS} user(s), ${WITH_GARMIN} with a Garmin login saved"
  ACTS=$(q "SELECT count(*) FROM activities"); LATEST=$(q "SELECT coalesce(max(date)::text,'-') FROM daily_summary")
  ok "synced data: ${ACTS} activities, latest daily summary ${LATEST}"
else
  fail "could not read the database"
fi
docker compose logs --tail 200 sync 2>/dev/null | grep -qi "login failed" && warn "sync log has Garmin login failures — see: docker compose logs sync"

echo
[ "$FAIL" -eq 0 ] && printf "${GREEN}${BOLD}All checks passed.${NC}\n" || { printf "${RED}${BOLD}${FAIL} check(s) failed.${NC}\n"; exit 1; }
