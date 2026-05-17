#!/usr/bin/env bash
# scripts/restore.sh — Restore from backup
# ─────────────────────────────────────────────────────────────────────────────
# IMPORTANT: STOP THE BOT BEFORE RESTORING
# Usage: ./scripts/restore.sh backups/20250516_143000
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

BACKUP_PATH="${1:-}"
DATA_DIR="${DATA_DIR:-./data}"

[[ -n "$BACKUP_PATH" ]] || { echo "Usage: $0 <backup-directory>"; exit 1; }
[[ -d "$BACKUP_PATH" ]] || { echo -e "${RED}Backup directory not found: $BACKUP_PATH${NC}"; exit 1; }

echo -e "${YELLOW}⚠ WARNING: This will overwrite your current trading state.${NC}"
echo -e "${YELLOW}  Make sure the bot is stopped: docker compose stop bot${NC}"
echo ""
echo "Restore from: $BACKUP_PATH"
echo "Restore to:   $DATA_DIR"
echo ""
read -r -p "Continue? (type YES to confirm): " CONFIRM
[[ "$CONFIRM" == "YES" ]] || { echo "Aborted."; exit 0; }

# Safety: check if bot is running
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "trading-bot$"; then
    echo -e "${RED}Bot is still running! Stop it first: docker compose stop bot${NC}"
    exit 1
fi

# Create safety backup of current state before restoring
SAFETY_BACKUP="./backups/pre_restore_$(date -u +%Y%m%d_%H%M%S)"
mkdir -p "$SAFETY_BACKUP"
cp "$DATA_DIR/"*.json "$SAFETY_BACKUP/" 2>/dev/null || true
cp "$DATA_DIR/"*.jsonl "$SAFETY_BACKUP/" 2>/dev/null || true
echo "Current state backed up to: $SAFETY_BACKUP"

# Restore critical JSON files
for f in active_trades.json risk_state.json; do
    SRC="$BACKUP_PATH/$f"
    if [[ -f "$SRC" ]]; then
        cp "$SRC" "$DATA_DIR/$f"
        echo -e "${GREEN}✓${NC} Restored: $f"
    else
        echo "  - $f not in backup (skipping)"
    fi
done

# Restore compressed JSONL files
for f in diary.jsonl alarms.jsonl decisions.jsonl; do
    SRC="$BACKUP_PATH/$f.gz"
    if [[ -f "$SRC" ]]; then
        gunzip -c "$SRC" > "$DATA_DIR/$f"
        echo -e "${GREEN}✓${NC} Restored: $f (decompressed)"
    fi
done

echo ""
echo -e "${GREEN}Restore complete.${NC}"
echo "Start the bot: docker compose start bot"
echo ""
echo -e "${YELLOW}IMPORTANT: After restart, the bot will reconcile with exchange state.${NC}"
echo -e "${YELLOW}The restored active_trades.json will be validated against live positions.${NC}"


---

#!/usr/bin/env bash
# scripts/update.sh — Update bot to latest version
# ─────────────────────────────────────────────────────────────────────────────
# Safe rolling update:
#   1. Manual backup
#   2. Pull new code
#   3. Rebuild image
#   4. Rolling restart (stop → start, not down → up to preserve volumes)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

echo "Starting safe bot update..."

# Step 1: Backup current state
echo ""
echo "1/4 Creating pre-update backup..."
./scripts/backup.sh
echo -e "${GREEN}Backup complete.${NC}"

# Step 2: Pull new code (if using git)
if [[ -d ".git" ]]; then
    echo ""
    echo "2/4 Pulling latest code..."
    git pull --ff-only
    echo -e "${GREEN}Code updated.${NC}"
else
    echo "2/4 Skipping git pull (no git repo detected — manual update assumed)"
fi

# Step 3: Rebuild image
echo ""
echo "3/4 Rebuilding Docker image..."
docker compose build --no-cache bot
echo -e "${GREEN}Image rebuilt.${NC}"

# Step 4: Rolling restart
echo ""
echo "4/4 Restarting bot service..."
docker compose stop bot
docker compose up -d bot
echo -e "${GREEN}Bot restarted.${NC}"

# Wait for health
echo ""
echo "Waiting for health check..."
MAX_WAIT=90; WAITED=0
while [[ $WAITED -lt $MAX_WAIT ]]; do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' trading-bot 2>/dev/null || echo "starting")
    [[ "$STATUS" == "healthy" ]] && break
    echo -ne "\r  ${WAITED}s (${STATUS})   "
    sleep 5; WAITED=$((WAITED+5))
done
echo ""
[[ "$STATUS" == "healthy" ]] && echo -e "${GREEN}✓ Bot healthy after update.${NC}" \
                              || echo -e "${YELLOW}⚠ Bot not yet healthy — check: docker compose logs -f bot${NC}"


---

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
