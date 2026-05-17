#!/usr/bin/env bash
# scripts/health_check.sh — Comprehensive health report
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

source .env 2>/dev/null || true

API_PORT="${API_PORT:-3000}"
API_SECRET="${API_SECRET:-}"

BOLD='\033[1m'; GREEN='\033[0;32m'; RED='\033[0;31m'
YELLOW='\033[1;33m'; NC='\033[0m'

head() { echo -e "\n${BOLD}═══ $* ═══${NC}"; }
ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
fail() { echo -e "  ${RED}✗${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }

head "Container Status"
docker compose ps 2>/dev/null || fail "Docker Compose not available"

head "Bot Health"
HEALTH=$(docker inspect --format='{{.State.Health.Status}}' trading-bot 2>/dev/null || echo "not running")
[[ "$HEALTH" == "healthy" ]] && ok "Container: $HEALTH" || fail "Container: $HEALTH"

head "API Connectivity"
HEALTH_URL="http://localhost:${API_PORT}/health?key=${API_SECRET}"
if curl -sf "$HEALTH_URL" -o /tmp/health_resp.json --max-time 5; then
    ok "API responding"
    cat /tmp/health_resp.json | python3 -m json.tool 2>/dev/null || cat /tmp/health_resp.json
else
    fail "API not responding at $HEALTH_URL"
fi

head "Active Alarms"
ALARM_URL="http://localhost:${API_PORT}/alarms?key=${API_SECRET}"
ALARMS=$(curl -sf "$ALARM_URL" --max-time 5 2>/dev/null | python3 -c "
import sys, json
data = json.load(sys.stdin)
alarms = data.get('alarms', [])
critical = [a for a in alarms if a.get('level','').upper() in ('CRITICAL','EMERGENCY','FAILED_NO_STOP')]
if critical:
    print(f'CRITICAL ALARMS: {len(critical)}')
    for a in critical[-5:]:
        print(f'  [{a.get(\"time\",\"\")}] {a.get(\"event\",\"\")}')
elif alarms:
    print(f'Recent alarms: {len(alarms)} (no critical)')
else:
    print('No recent alarms')
" 2>/dev/null || echo "Could not fetch alarms")
echo "  $ALARMS"

head "Disk Space"
df -h / | tail -1 | awk '{
    used=$5
    gsub(/%/,"",used)
    if (used+0 > 85) print "  \033[0;31m✗ CRITICAL: " $5 " used (" $4 " free)\033[0m"
    else if (used+0 > 70) print "  \033[1;33m! WARNING: " $5 " used (" $4 " free)\033[0m"
    else print "  \033[0;32m✓ " $5 " used (" $4 " free)\033[0m"
}'

head "Backup Status"
LATEST_BACKUP=$(ls -1t ./backups/ 2>/dev/null | head -1 || echo "none")
if [[ "$LATEST_BACKUP" == "none" ]]; then
    warn "No backups found"
else
    BACKUP_AGE=$(( ($(date +%s) - $(date -r "./backups/$LATEST_BACKUP" +%s 2>/dev/null || echo 0)) / 60 ))
    [[ $BACKUP_AGE -lt 10 ]] && ok "Latest backup: $LATEST_BACKUP (${BACKUP_AGE}m ago)" \
                              || warn "Latest backup: $LATEST_BACKUP (${BACKUP_AGE}m ago)"
fi

head "Log Sizes"
for f in data/active_trades.json data/diary.jsonl data/alarms.jsonl logs/trading.log logs/llm_requests.log; do
    if [[ -f "$f" ]]; then
        SIZE=$(du -sh "$f" 2>/dev/null | cut -f1)
        echo "  $f: $SIZE"
    fi
done

echo ""
echo -e "${BOLD}Health check complete.${NC}"
