# Telegram Alerts Setup Guide
**Hyperliquid AI Trading Bot — Push Notification System**

> **Why this matters**: The bot runs unattended 24/7. Without Telegram alerts, the only way to know about critical events (failed stop-loss, circuit breaker, exchange disconnect) is to check log files manually. For live trading, push alerts are **strongly recommended**.

---

## Quick Setup (5 minutes)

### Step 1: Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a name: `MyHyperliquidBot`
4. Choose a username: `myhyperliquid_bot` (must end in `bot`)
5. Copy the **API token** you receive (format: `1234567890:AAF...`)

### Step 2: Get Your Chat ID

1. Start a conversation with your new bot (search for it in Telegram, click Start)
2. Send any message to it (e.g. "hello")
3. Open this URL in your browser (replace `TOKEN` with your token):
   ```
   https://api.telegram.org/botTOKEN/getUpdates
   ```
4. Find `"chat":{"id":123456789}` — that number is your Chat ID

### Step 3: Configure .env

```dotenv
TELEGRAM_BOT_TOKEN=1234567890:AAFxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=123456789
BOT_NAME=MyHyperliquidBot   # Displayed in every alert
```

### Step 4: Test

```bash
# Restart the bot to pick up new settings
docker compose restart bot

# Watch logs for Telegram confirmation
docker compose logs -f bot | grep -i telegram
```

You should receive a Telegram message: `🟢 [MyHyperliquidBot] Bot is online and initialising.`

---

## Alert Reference

### System Lifecycle Alerts

| Alert Code | Severity | Trigger | Action Required |
|-----------|----------|---------|----------------|
| `BOT_STARTED` | INFO | Bot comes online | None |
| `BOT_RESTARTED` | WARNING | Docker restart detected | Monitor for repeated restarts |
| `BOT_SHUTDOWN` | INFO | Graceful shutdown | None |
| `DOCKER_UNHEALTHY` | CRITICAL | Health check failing for 3+ cycles | Check logs immediately |
| `VPS_STARTUP` | INFO | Container initialising | None |

### Exchange Connectivity

| Alert Code | Severity | Trigger | Action Required |
|-----------|----------|---------|----------------|
| `EXCHANGE_DISCONNECT` | CRITICAL | API calls failing after 3 retries | Check Hyperliquid status page |
| `EXCHANGE_RECONNECT` | INFO | Reconnection successful | None |
| `API_FAILURE` | WARNING | Single API call failed | Bot will retry — monitor |
| `API_THROTTLED` | WARNING | Rate limit hit | Bot will back off automatically |

### Reconciliation

| Alert Code | Severity | Trigger | Action Required |
|-----------|----------|---------|----------------|
| `STARTUP_RECONCILIATION_FAILED` | CRITICAL | Could not verify positions at startup | Check exchange connectivity; bot is retrying |
| `RECONCILIATION_STALE` | WARNING | Position state may be stale | Bot is refreshing — monitor |

### Trading Events

| Alert Code | Severity | Trigger | Action Required |
|-----------|----------|---------|----------------|
| `POSITION_OPENED` | INFO | New position entered | Review if unexpected |
| `POSITION_CLOSED` | INFO | Position closed (TP/SL/force) | Review PnL |
| `ORDER_REJECTED` | WARNING | Exchange rejected order | Check margin/limits |
| `PARTIAL_FILL` | WARNING | Order partially filled | Bot handles automatically |

### 🚨 Critical Risk Alerts

These require your attention. Do not ignore them.

| Alert Code | Severity | Trigger | WHAT TO DO |
|-----------|----------|---------|------------|
| `FAILED_NO_STOP` | 🆘 EMERGENCY | Stop-loss could not be placed after 3 attempts AND market close also failed | **Log into Hyperliquid NOW. Close the position manually or set SL.** Position is live and unprotected. |
| `STOP_LOSS_REPAIR_FAILED` | 🆘 EMERGENCY | Stop-loss repair retries exhausted — emergency close being attempted | **Monitor — emergency close in progress. If close fails, you'll get FAILED_NO_STOP.** |
| `CIRCUIT_BREAKER_ACTIVATED` | 🚨 CRITICAL | Daily drawdown limit reached — no new entries | Existing positions continue with SL. No new entries today. Review losses before re-enabling. |
| `HIGH_DRAWDOWN_WARNING` | ⚠️ WARNING | Drawdown at 75% of circuit breaker limit | Consider reducing exposure manually |
| `LIQUIDATION_DANGER` | 🆘 EMERGENCY | Position approaching liquidation price | Close position or add margin immediately |
| `CRITICAL_RISK_EVENT` | 🆘 EMERGENCY | Multiple risk systems flagging simultaneously | Immediate human intervention required |
| `FORCE_CLOSE_FAILED` | 🆘 EMERGENCY | Emergency market close order failed | Log into exchange and close position manually |

### Strategy / AI

| Alert Code | Severity | Trigger | Action Required |
|-----------|----------|---------|----------------|
| `STRATEGY_FAILURE` | WARNING | Single strategy cycle produced no decisions | Bot holding — monitor |
| `REPEATED_AI_FAILURE` | CRITICAL | 2+ consecutive cycles with no actionable output | Check AI API status; bot is holding safely |
| `REPEATED_STRATEGY_FAILURE` | CRITICAL | Both AI and algo strategies failing | Check logs for error details |

### Manual Intervention

| Alert Code | Severity | Always requires human action |
|-----------|----------|------------------------------|
| `MANUAL_INTERVENTION_REQUIRED` | 🆘 EMERGENCY | Always |
| `UNEXPECTED_EXCEPTION` | 🚨 CRITICAL | Check logs for root cause |

---

## Alert Behaviour

### Deduplication
Identical alerts (same code + asset + message prefix) are suppressed for **60 seconds**. You won't be spammed with the same alert every 5 minutes if an issue persists — but you'll get one alert per minute at most for a recurring issue.

**EMERGENCY alerts bypass deduplication** — every FAILED_NO_STOP event sends immediately.

### Rate Limiting
Maximum 18 messages per minute (Telegram's limit is 20 — 2 are reserved as buffer). In extremely noisy situations, older alerts queue up and deliver in order as rate allows. EMERGENCY alerts bypass rate limiting.

### What Happens If Telegram Is Down
The bot continues trading. Alert failures are logged at WARNING level but never crash the bot or block execution. If Telegram is unreachable for extended periods, check your internet connectivity on the VPS.

---

## Advanced: Group Chat Alerts

To send alerts to a Telegram group (useful for monitoring alongside a partner):

1. Add your bot to the group
2. Send a message in the group
3. Fetch updates: `https://api.telegram.org/botTOKEN/getUpdates`
4. Find the group chat ID (negative number, e.g. `-100123456789`)
5. Set `TELEGRAM_CHAT_ID=-100123456789`

---

## Troubleshooting

**No startup message received:**
- Verify token with: `curl "https://api.telegram.org/botTOKEN/getMe"`
- Verify you sent a message to the bot first (required for bots to message you)
- Check logs: `docker compose logs bot | grep -i telegram`

**Getting duplicate alerts:**
- This is expected for EMERGENCY severity (dedup bypassed)
- For other alerts, dedup window is 60 seconds

**Too many alerts:**
- Adjust thresholds in `.env` (e.g. raise drawdown warning threshold)
- POSITION_OPENED/CLOSED can be high volume — consider a separate bot for INFO-level alerts

**403 Forbidden error:**
- Your bot token is invalid or the bot was deleted
- Create a new bot via @BotFather
