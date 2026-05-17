# Troubleshooting Guide
**Hyperliquid AI Trading Bot — Problem Resolution**

---

## Quick Diagnostics

Run this first for any problem:
```bash
./scripts/health_check.sh
docker compose ps
docker compose logs --tail=50 bot
```

---

## Problem Index

| Symptom | Go to Section |
|---------|--------------|
| Bot won't start | [Bot Fails to Start](#bot-fails-to-start) |
| No Telegram alerts | [Telegram Not Working](#telegram-not-working) |
| Bot not trading | [Bot Running But Not Trading](#bot-running-but-not-trading) |
| FAILED_NO_STOP alarm | [Emergency: FAILED_NO_STOP](#emergency-failed_no_stop) |
| Circuit breaker active | [Circuit Breaker Activated](#circuit-breaker-activated) |
| Wrong position sizes | [Wrong Position Sizes](#wrong-position-sizes) |
| Bot keeps restarting | [Crash Loop](#crash-loop) |
| API endpoint 401 | [API Authentication](#api-authentication) |
| High disk usage | [Disk Space Issues](#disk-space-issues) |
| Paper vs live discrepancy | [Paper Trading Differences](#paper-trading-differences) |
| Exchange connection errors | [Exchange Connectivity](#exchange-connectivity) |
| AI API errors | [AI Provider Issues](#ai-provider-issues) |

---

## Bot Fails to Start

### Symptom
`docker compose up -d` exits, or container shows `Exited` immediately.

### Diagnosis
```bash
docker compose logs bot | tail -100
```

### Common Causes

**Missing .env variables**
```
ERROR: HYPERLIQUID_PRIVATE_KEY not set
```
→ Open `.env` and fill in the missing variable.

**Invalid private key format**
```
ERROR: Private key must be 64 hex characters
```
→ Your key should be: `0x` followed by exactly 64 hex characters. Get it from your Hyperliquid agent key setup.

**Invalid ASSETS**
```
ERROR: Asset 'BTCC' not found in Hyperliquid universe
```
→ Check valid symbols at hyperliquid.xyz. Use `BTC`, `ETH`, `SOL` etc. (not `BTC-USD`, `XBTUSD`).

**Port already in use**
```
ERROR: address already in use :::3000
```
→ Another process is using port 3000.
```bash
lsof -ti:3000 | xargs kill -9
# Or change API_PORT in .env
```

**Python dependency error**
→ Rebuild the image:
```bash
docker compose build --no-cache bot
docker compose up -d
```

---

## Telegram Not Working

### Symptom
No startup message received after deploy.

### Diagnosis Steps

**Step 1: Verify bot token**
```bash
source .env
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"
```
Should return `"ok": true`. If not, token is invalid — create new bot via @BotFather.

**Step 2: Verify you've started a chat**
- Open Telegram, find your bot by username
- Click START or send it any message
- Only then can it message you back

**Step 3: Verify chat ID**
```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates"
```
Find `"chat":{"id":YOUR_ID}` in the response. Must match `TELEGRAM_CHAT_ID` in `.env`.

**Step 4: Check bot logs**
```bash
docker compose logs bot | grep -i telegram
```

**Step 5: Test send manually**
```bash
source .env
curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -H "Content-Type: application/json" \
  -d "{\"chat_id\": \"${TELEGRAM_CHAT_ID}\", \"text\": \"test\"}"
```

---

## Bot Running But Not Trading

### Symptom
Bot is healthy, no errors, but diary shows only "hold" decisions or no trades for many cycles.

### Diagnosis
```bash
# Check decision log
tail -20 data/decisions.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    d = json.loads(line.strip())
    print(f\"{d.get('time','')[:19]} {d.get('asset','')} → {d.get('action','')} (confidence: {d.get('confidence',0):.2f})\")
"

# Check AI provider errors
docker compose logs bot | grep -i "ai\|openai\|anthropic\|strategy"

# Check circuit breaker
curl "http://localhost:3000/status?key=${API_SECRET}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print('Circuit breaker:', d['risk']['circuit_breaker_active'])
print('Drawdown:', d['risk']['drawdown_pct'], '%')
"
```

### Common Causes

**Circuit breaker is active** → See [Circuit Breaker Activated](#circuit-breaker-activated)

**AI returning all holds** → Market conditions or strategy constraints causing the AI to hold.
This is correct behavior — the bot should not force trades. Check `decisions.jsonl` for the AI's rationale.

**AI API rate limited or down**
```bash
docker compose logs bot | grep -i "rate limit\|429\|openai\|api"
```
→ Check your AI provider's status page and API quota.

**DRY_RUN and capital too small** → If `DRY_RUN_INITIAL_BALANCE` is very small, position sizing may round to zero. Increase it.

**ASSETS not trading hours** → Some assets have lower liquidity at certain times. Holds are expected.

---

## EMERGENCY: FAILED_NO_STOP

### What This Means
A position is open on the exchange with **no stop-loss**. The bot tried 3 times to set a SL and failed. It then tried to market-close the position and that also failed. The position is completely unprotected.

### IMMEDIATE ACTION REQUIRED

**Step 1: Open Hyperliquid immediately**
- Go to app.hyperliquid.xyz
- Log in with your wallet

**Step 2: Find the position**
- Go to Portfolio → Positions
- Find the asset listed in the FAILED_NO_STOP alarm

**Step 3: Set a stop-loss OR close entirely**

Option A — Set stop-loss (keep position):
- Click on the position
- Add a stop-loss order at an acceptable price
- Confirm the order

Option B — Close position immediately (safest):
- Click "Market Close" on the position
- Confirm

**Step 4: After securing the position**
```bash
# Restart bot to re-reconcile state
docker compose restart bot
docker compose logs -f bot | head -50
```

**Step 5: Investigate why SL failed**
```bash
docker compose logs bot | grep -i "stop\|sl\|failed_no_stop\|flatten"
tail -20 data/alarms.jsonl
```

Common causes:
- Exchange API was temporarily unreachable
- Margin too thin to post the SL order
- Price moved through the SL level before it could be set (gap move)

---

## Circuit Breaker Activated

### What This Means
The bot's daily loss has reached the configured threshold (`DAILY_LOSS_CIRCUIT_BREAKER_PCT`). All new entries are blocked for the rest of the trading day. **Existing positions continue with their stop-losses** — they are NOT affected.

### This Is Normal Behavior
The circuit breaker is a safety feature. If it fires, the bot did its job.

### What To Do

**Option 1: Wait for next day**
The circuit breaker resets at UTC midnight. No action needed. The bot will resume trading automatically.

**Option 2: Manual reset (if you're confident)**
```bash
# Stop bot
docker compose stop bot

# Edit risk_state.json and reset the breaker
python3 -c "
import json
with open('data/risk_state.json') as f:
    state = json.load(f)
state['circuit_breaker_active'] = False
state['daily_high_balance'] = state.get('current_balance', state.get('daily_high_balance'))
with open('data/risk_state.json', 'w') as f:
    json.dump(state, f, indent=2)
print('Circuit breaker reset')
"

docker compose start bot
```

**Only do this if you understand WHY the circuit breaker fired** and you believe it was a transient issue, not a strategy failure.

---

## Wrong Position Sizes

### Symptom
Positions are much larger or smaller than expected.

### Check your configuration
```bash
source .env
echo "AI Capital %: ${AI_CAPITAL_PCT}"
echo "Max Position %: ${MAX_POSITION_PCT}"
echo "Max Leverage: ${MAX_LEVERAGE}"
echo "Safe Mode: ${SAFE_RETAIL_MODE}"
```

### With SAFE_RETAIL_MODE=true (conservative preset)
- Max position: 5% of account per trade
- Max leverage: 3x
- These override MAX_LEVERAGE and AI_CAPITAL_PCT if they're set higher

### With SAFE_RETAIL_MODE=true (default preset)
- Max position: 10% of account
- Max leverage: 5x

### Calculating expected position size
```
Position notional = account_value × (AI_CAPITAL_PCT / 100) × AI_confidence_factor
Margin required   = Position notional / leverage
```

Example: $10,000 account, 5% capital, AI confidence 0.7, 3x leverage:
```
Notional = $10,000 × 0.05 × 0.7 = $350 notional
Margin   = $350 / 3 = $117 of actual margin used
```

---

## Crash Loop

### Symptom
Bot keeps restarting every few minutes. `docker compose ps` shows the bot repeatedly cycling.

### Diagnosis
```bash
docker compose logs bot | tail -200 | grep -E "ERROR|CRITICAL|Traceback|Exception"
```

### Common Causes

**Persistent startup reconciliation failure**
Exchange is unreachable at startup. Bot retries but Docker's restart policy restarts the whole container.
→ Check your internet connection from the VPS: `curl -I https://api.hyperliquid.xyz`
→ Check Hyperliquid status: https://status.hyperliquid.xyz

**Corrupted state file**
```bash
python3 -m json.tool data/active_trades.json  # should not error
python3 -m json.tool data/risk_state.json     # should not error
```
If these error, restore from backup: `./scripts/restore.sh backups/LATEST`

**Out of memory**
```bash
free -h
docker stats trading-bot
```
If memory usage >90%, the container is being OOM-killed. Upgrade VPS RAM or reduce `MAX_WORKERS`.

---

## API Authentication

### Symptom
```
HTTP 401 Unauthorized
```

### Fixes
1. Make sure you're passing the correct `API_SECRET` from your `.env`
2. The secret goes in the query string: `?key=YOUR_SECRET`
3. Check for accidental spaces or newlines in `API_SECRET`
4. Verify the secret matches exactly (case-sensitive)

```bash
source .env
curl "http://localhost:3000/health?key=${API_SECRET}"
```

---

## Disk Space Issues

### Symptom
Bot stops with "No space left on device" error.

### Immediate Fix
```bash
# Check what's taking space
du -sh ./logs/* ./data/* ./backups/* 2>/dev/null | sort -h

# Prune old backups (keeps last 10)
ls -1t backups/ | tail -n +11 | xargs -I{} rm -rf backups/{}

# Compress old logs manually
gzip -9 data/diary.jsonl.old 2>/dev/null || true

# Check Docker images
docker system df
docker image prune -f   # remove unused images
```

### Long-term Fix
- Ensure the logrotate sidecar is running: `docker compose ps logrotate`
- Reduce `BACKUP_RETAIN_COUNT` in `.env`
- Add more disk to VPS or upgrade tier

---

## Paper Trading Differences

### "The bot behaves differently in paper mode vs live"

This is **expected and documented**. Key differences:

| Behavior | Paper Mode | Live Mode |
|----------|-----------|-----------|
| Fill price | Exactly `current_price` at time of order | Actual fill with slippage |
| Partial fills | Not simulated | Can happen on thin markets |
| Margin deduction | Simulated (with fix applied) | Real exchange margin |
| Liquidation | Simulated estimate | Real liquidation engine |
| Order rejection | Rare (only margin check) | Can be rejected by exchange |

### "Live trading needs more margin than paper"

This means the margin fix (`dry_run_margin_patch.py`) was applied correctly. Paper mode now correctly simulates margin deduction. If you see rejections in live that didn't happen in paper, check:
1. Your live account balance matches your paper `DRY_RUN_INITIAL_BALANCE`
2. Your leverage settings are the same in both modes

---

## Exchange Connectivity

### Symptom
Logs show repeated exchange API errors.

```bash
docker compose logs bot | grep -i "hyperliquid\|exchange\|retry\|disconnect"
```

### Diagnosis
```bash
# Test from VPS
curl -I "https://api.hyperliquid.xyz"
ping -c 4 api.hyperliquid.xyz

# Check Hyperliquid status
curl "https://stats.hyperliquid.xyz/uptime" 2>/dev/null || echo "Check status.hyperliquid.xyz"
```

### If Exchange Is Down
The bot will retry automatically on every cycle. No manual action needed. When exchange comes back, the bot resumes. Check `/health` to confirm `exchange_connected: true` after recovery.

---

## AI Provider Issues

### Symptom
Logs show OpenAI / Anthropic API errors.

```bash
docker compose logs bot | grep -iE "openai|anthropic|gemini|api.*key|rate.limit|429"
```

### Common Fixes

**Expired API key**
→ Generate a new key at platform.openai.com or console.anthropic.com
→ Update `OPENAI_API_KEY` in `.env`
→ `docker compose restart bot`

**Rate limit (429)**
→ The bot has retry logic — usually self-resolves
→ If persistent, upgrade your API tier

**Quota exceeded**
→ Check usage at platform.openai.com/usage
→ Set a higher usage limit or add billing

**Wrong model name**
→ Check `AI_MODEL` in `.env` against current available models

---

## Getting Support

When contacting support, include:

```bash
# Generate support bundle
{
  echo "=== Docker Status ==="
  docker compose ps
  echo "=== Last 100 Bot Logs ==="
  docker compose logs --tail=100 bot
  echo "=== Health Check ==="
  curl -s "http://localhost:3000/health?key=${API_SECRET}" | python3 -m json.tool
  echo "=== Recent Alarms ==="
  tail -20 data/alarms.jsonl
} > support_bundle.txt 2>&1

echo "Support bundle saved to support_bundle.txt"
```

**Remove sensitive data before sending:**
```bash
# Redact private keys and secrets
sed -i 's/0x[0-9a-fA-F]\{64\}/REDACTED_PRIVATE_KEY/g' support_bundle.txt
sed -i "s/${API_SECRET}/REDACTED_SECRET/g" support_bundle.txt
```
