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

