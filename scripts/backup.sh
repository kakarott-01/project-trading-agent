#!/usr/bin/env bash
# scripts/backup.sh — Manual backup trigger
# ─────────────────────────────────────────────────────────────────────────────
# Creates an immediate backup of all state files.
# Safe to run at any time — does not stop the bot.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
DATA_DIR="${DATA_DIR:-./data}"
TS=$(date -u +"%Y%m%d_%H%M%S")
DST="$BACKUP_DIR/manual_$TS"

mkdir -p "$DST"

echo "Creating manual backup → $DST"

# Critical state files
for f in active_trades.json risk_state.json; do
    SRC="$DATA_DIR/$f"
    if [[ -f "$SRC" ]]; then
        cp "$SRC" "$DST/$f"
        echo "  ✓ $f"
    else
        echo "  - $f (not found — may not exist yet)"
    fi
done

# Compress JSONL files
for f in diary.jsonl alarms.jsonl decisions.jsonl; do
    SRC="$DATA_DIR/$f"
    if [[ -f "$SRC" ]]; then
        gzip -c "$SRC" > "$DST/$f.gz"
        echo "  ✓ $f (compressed)"
    fi
done

# Write manifest
cat > "$DST/manifest.json" <<EOF
{
  "timestamp": "$TS",
  "type": "manual",
  "source": "$DATA_DIR"
}
EOF

echo ""
echo "Backup complete: $DST"
echo "Size: $(du -sh "$DST" | cut -f1)"
