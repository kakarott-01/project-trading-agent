# Backup & Recovery Guide
**Hyperliquid AI Trading Bot — Data Protection**

---

## What Gets Backed Up

| File | Location | Importance | What Happens Without It |
|------|----------|------------|------------------------|
| `active_trades.json` | `data/` | 🔴 CRITICAL | Bot starts fresh — reconciles from exchange, but loses local metadata (confidence scores, exit plans, source) |
| `risk_state.json` | `data/` | 🔴 CRITICAL | Bot starts fresh — daily circuit breaker state lost, drawdown watermark reset |
| `diary.jsonl` | `data/` | 🟡 IMPORTANT | Lose trade history for this session |
| `alarms.jsonl` | `data/` | 🟡 IMPORTANT | Lose alarm history |
| `decisions.jsonl` | `data/` | 🟢 USEFUL | Lose AI decision log |

> **Key insight**: Even without backups, the bot will reconcile safely from the exchange on restart. You lose *local* context (why a position was opened, what the AI decided), but the **bot will not open duplicate positions or ignore existing ones** — exchange truth always wins.

---

## Automatic Backup Service

The backup sidecar service runs automatically when you start the bot:

```bash
docker compose up -d   # starts bot + backup + logrotate
docker compose ps      # verify backup container is running
```

**Backup schedule**: Every 5 minutes (configurable via `BACKUP_INTERVAL_SECONDS`)

**Backup retention**: Last 144 backups (12 hours at 5-min intervals). Configurable via `BACKUP_RETAIN_COUNT`.

**Backup location**: `./backups/TIMESTAMP/`

---

## Off-VPS Backup (Strongly Recommended)

For maximum safety, back up to a remote location. The backup service supports [rclone](https://rclone.org), which can upload to:
- Amazon S3
- Backblaze B2 (cheapest — free 10 GB)
- Cloudflare R2 (free 10 GB)
- Google Drive
- Any S3-compatible storage

### Setup with Backblaze B2 (Free Tier)

1. Create a Backblaze B2 account at backblaze.com
2. Create a bucket named `trading-bot-backups`
3. Create an Application Key with write access

```bash
# Install rclone on VPS
curl https://rclone.org/install.sh | sudo bash

# Configure rclone
rclone config
# Choose: n (new remote), name: backblaze, type: b2
# Enter your Account ID and Application Key

# Test upload
rclone copy ./backups/latest backblaze:trading-bot-backups/test/
```

4. Add to `.env`:
```dotenv
RCLONE_REMOTE=backblaze:trading-bot-backups
```

5. Restart backup service:
```bash
docker compose restart backup
```

---

## Manual Backup

Create an immediate backup at any time (bot keeps running):

```bash
./scripts/backup.sh
```

**When to run manually**:
- Before updating the bot
- Before changing configuration
- Before going on a trip (unreachable for extended time)
- After a CRITICAL alarm event

---

## Recovery Procedures

### Scenario 1: Bot Restarted Normally (Most Common)

No action needed. The bot automatically:
1. Loads `active_trades.json` from disk
2. Reconciles against exchange state
3. Corrects any discrepancies
4. Resumes trading

### Scenario 2: active_trades.json Corrupted

Signs: Bot fails to start, JSON parse errors in logs.

```bash
# Stop bot
docker compose stop bot

# Restore from latest backup
ls backups/   # find latest timestamp
./scripts/restore.sh backups/20250516_143000

# Start bot
docker compose start bot
```

The bot will reconcile the restored state against exchange truth.

### Scenario 3: VPS Crashed, Restoring to New VPS

```bash
# 1. Set up new VPS (follow production_deployment.md)
# 2. Upload your bot files
scp -r trading-bot/ tradingbot@NEW_VPS_IP:~/

# 3. If you have off-VPS backups (rclone/B2):
rclone copy backblaze:trading-bot-backups/latest/ ~/trading-bot/data/

# 4. Deploy normally
cd ~/trading-bot
./scripts/deploy.sh
```

### Scenario 4: Complete Data Loss (No Backups)

```bash
# Bot will start with empty state
# Exchange reconciliation will find existing positions
# Local metadata is lost but positions are safe
docker compose up -d
```

What you lose: local trade diary, alarm history, risk state watermarks, AI decision rationale.

What you don't lose: actual exchange positions (these are on Hyperliquid's servers).

### Scenario 5: risk_state.json Corrupted (Circuit Breaker State Lost)

```bash
# Stop bot
docker compose stop bot

# Option A: Restore from backup (preserves daily watermark)
./scripts/restore.sh backups/LATEST_BACKUP_DIR

# Option B: Delete and let bot rebuild from scratch
rm data/risk_state.json
# Bot will start with fresh risk state — daily high watermark resets to current balance

docker compose start bot
```

---

## Backup Verification

Periodically verify your backups are valid:

```bash
# Check backup directory
ls -la backups/ | head -10

# Verify a backup's JSON is valid
python3 -c "
import json, pathlib
for path in sorted(pathlib.Path('backups').rglob('active_trades.json'))[-3:]:
    try:
        with open(path) as f:
            data = json.load(f)
        print(f'✓ {path} — {len(data)} trades')
    except Exception as e:
        print(f'✗ {path} — INVALID: {e}')
"
```

---

## .env Backup

Your `.env` file contains credentials that are not backed up by the backup service (for security). Store it separately:

**Options** (choose one):
- Password manager (1Password, Bitwarden) — paste the file contents as a secure note
- Encrypted file on a separate device
- Print and store securely

**Never**: Store in git, cloud drives, email drafts, or unencrypted notes.

---

## Backup Service Troubleshooting

```bash
# Check backup service status
docker compose logs backup

# Check backup directory
ls -la backups/

# Manual backup if service is down
./scripts/backup.sh

# Check if backup service is running
docker compose ps backup
```

If the backup service fails repeatedly:
1. Check disk space: `df -h`
2. Check permissions: `ls -la data/ backups/`
3. Check rclone config if using remote backup: `rclone config show`
