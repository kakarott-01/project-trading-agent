# Monitoring Guide
**Hyperliquid AI Trading Bot — Operational Monitoring**

---

## Monitoring Philosophy

This bot is designed for **unattended operation**. You should not need to watch it constantly. The monitoring system is designed to:

1. **Push** critical alerts to you (Telegram) so you can act immediately
2. **Pull** when you want a current status snapshot (API endpoints)
3. **Record** everything so you can audit decisions after the fact (JSONL logs)

> Rule of thumb: If Telegram is silent, the bot is working normally.

---

## API Endpoints Reference

All endpoints require authentication: append `?key=YOUR_API_SECRET` to every request.

**Base URL**: `http://localhost:3000` (or your configured port)

**Quick access via SSH tunnel** (from your local machine):
```bash
ssh -L 3000:127.0.0.1:3000 -N tradingbot@YOUR_VPS_IP &
```

---

### `/health` — System Health

```bash
curl "http://localhost:3000/health?key=YOUR_SECRET"
```

```json
{
  "status": "ok",
  "uptime_seconds": 86400,
  "last_cycle_at": "2025-05-16T14:30:00Z",
  "last_cycle_ago_seconds": 45,
  "cycle_health": "ok",
  "exchange_connected": true,
  "circuit_breaker_active": false,
  "dry_run": false,
  "version": "1.0.0"
}
```

**What to watch**: `cycle_health`, `last_cycle_ago_seconds` (>600 means bot may be stuck), `exchange_connected`.

---

### `/status` — Full Trading Status

```bash
curl "http://localhost:3000/status?key=YOUR_SECRET"
```

```json
{
  "account": {
    "balance_usdc": 9850.42,
    "account_value": 10124.17,
    "unrealized_pnl": 273.75,
    "daily_drawdown_pct": 1.2
  },
  "positions": [
    {
      "asset": "BTC",
      "direction": "LONG",
      "size": 0.004,
      "entry_price": 67500.0,
      "current_price": 68100.0,
      "unrealized_pnl": 2.40,
      "stop_loss": 65925.0,
      "take_profit": 70875.0,
      "leverage": 3,
      "status": "open_position"
    }
  ],
  "risk": {
    "circuit_breaker_active": false,
    "daily_high_balance": 10000.0,
    "current_balance": 9850.42,
    "drawdown_pct": 1.49,
    "total_exposure_pct": 18.5
  },
  "strategy": {
    "last_decision_at": "2025-05-16T14:30:00Z",
    "last_ai_decision": "LONG BTC (confidence: 0.72)",
    "consecutive_holds": 0
  },
  "system": {
    "uptime_seconds": 86400,
    "last_cycle_at": "2025-05-16T14:30:00Z",
    "cycle_interval_seconds": 300,
    "reconciliation_status": "ok"
  }
}
```

---

### `/alarms` — Active Alarms

```bash
curl "http://localhost:3000/alarms?key=YOUR_SECRET"
```

This is the most important endpoint for manual monitoring. Check it:
- When Telegram alerts are not configured
- When you want to see historical alarm context
- After any unusual bot behavior

```json
{
  "alarms": [
    {
      "time": "2025-05-16T14:32:00Z",
      "event": "CIRCUIT_BREAKER_ACTIVATED",
      "level": "CRITICAL",
      "asset": null,
      "message": "Daily drawdown reached 8.1% — entries blocked",
      "resolved": false
    }
  ],
  "unresolved_count": 1,
  "has_critical": true
}
```

**Critical**: If `has_critical: true` and `FAILED_NO_STOP` appears, act immediately.

---

### `/diary` — Trade Diary

```bash
curl "http://localhost:3000/diary?key=YOUR_SECRET&limit=20"
```

Returns the last N diary entries — all significant events the bot logged.

---

### `/positions` — Active Trades Only

```bash
curl "http://localhost:3000/positions?key=YOUR_SECRET"
```

---

### `/logs` — Recent Log Lines

```bash
curl "http://localhost:3000/logs?path=trading.log&key=YOUR_SECRET"
```

Available log files: `trading.log`, `decisions.jsonl`, `llm_requests.log`

---

## Monitoring Dashboard

Access the built-in monitoring dashboard via SSH tunnel:

```bash
# From your local machine:
ssh -L 3000:127.0.0.1:3000 -N tradingbot@YOUR_VPS_IP
# Open browser: http://localhost:3000/dashboard?key=YOUR_SECRET
```

The dashboard shows:
- Account equity and balance
- Open positions with current PnL
- Risk metrics (drawdown, circuit breaker status)
- Recent alarms (highlighted by severity)
- Last 20 trades from diary
- Bot uptime and last cycle time
- Exchange connectivity status

---

## Command-Line Monitoring

### Live Log Stream

```bash
# All logs (most verbose)
docker compose logs -f bot

# Filter for errors and critical only
docker compose logs -f bot 2>&1 | grep -E "CRITICAL|ERROR|WARN"

# Filter for trading events
docker compose logs -f bot 2>&1 | grep -E "position|trade|order|SL|TP"
```

### Trade Diary (Human-Readable)

```bash
# Last 20 events
tail -20 data/diary.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    d = json.loads(line.strip())
    print(f\"{d.get('time','')[:19]} [{d.get('event','').upper()}] {d.get('asset','')} — {d.get('message','')[:80]}\")
"
```

### Current Positions (Quick Check)

```bash
cat data/active_trades.json | python3 -c "
import sys, json
trades = json.load(sys.stdin)
if not trades:
    print('No active trades')
else:
    for t in trades:
        print(f\"{t.get('asset')} {t.get('direction','').upper()} | Status: {t.get('status')} | Entry: {t.get('entry_price',0):.4f}\")
"
```

### Recent Alarms

```bash
tail -10 data/alarms.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    a = json.loads(line.strip())
    lvl = a.get('level','INFO')
    prefix = '🚨' if 'CRITICAL' in lvl.upper() or 'EMERGENCY' in lvl.upper() else '⚠️' if 'WARNING' in lvl.upper() else 'ℹ️'
    print(f\"{prefix} {a.get('time','')[:19]} {a.get('event','')} — {a.get('message','')[:100]}\")
"
```

---

## Key Metrics to Watch

### Daily Check (2 minutes)
- Telegram has no EMERGENCY/CRITICAL alerts → ✅ Good
- `/health` returns `status: ok` → ✅ Good
- `/alarms` shows no unresolved FAILED_NO_STOP → ✅ Good

### Weekly Review (10 minutes)
1. Run `./scripts/health_check.sh`
2. Check disk usage: `df -h`
3. Review weekly PnL from diary: `grep "position_closed" data/diary.jsonl | tail -20`
4. Confirm backups running: `ls -la backups/ | head -10`

### If Something Looks Wrong

| Symptom | First Check | Likely Cause |
|---------|-------------|--------------|
| No Telegram for >1 hour | `/health` cycle health | Bot restart, or exchange down |
| Last cycle >10 min ago | `docker compose ps` | Bot crash, restarting |
| FAILED_NO_STOP alarm | Hyperliquid UI | SL rejected — close manually |
| Circuit breaker active | `/status` drawdown | Normal protection — review losses |
| Balance unexpected | `/status` account | Check positions and PnL |
| High disk usage | `df -h`, `du -sh logs/*` | Log rotation failed — check logrotate service |

---

## Interpreting Bot Decisions

The bot writes AI decisions to `decisions.jsonl`. To see what the AI decided and why:

```bash
tail -5 data/decisions.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    d = json.loads(line.strip())
    print('─' * 60)
    print(f\"Asset:  {d.get('asset')}\")
    print(f\"Action: {d.get('action')}\")
    print(f\"Confidence: {d.get('confidence')}\")
    print(f\"Leverage: {d.get('leverage')}\")
    print(f\"Rationale: {d.get('rationale','')[:200]}\")
"
```

---

## What Normal Looks Like

A healthy bot running 5-minute cycles on 2 assets will:

- Log **1 diary entry per asset per cycle** (some cycles skip if position unchanged)
- Show **no CRITICAL alarms** in normal market conditions
- Show the **circuit breaker inactive** unless a large loss day occurred
- Show **positions with SL set** — never "open_position" status without a stop
- Report **last cycle** within the last 10 minutes

If you see `status: failed_no_stop` in `active_trades.json`, act immediately.
