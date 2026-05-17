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

