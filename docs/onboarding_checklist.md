# Customer Onboarding Checklist
**Hyperliquid AI Trading Bot — Complete Setup Verification**

Work through this checklist top to bottom before going live with real capital.
Every item must be checked. Do not skip steps.

---

## PHASE 1 — VPS Setup

- [ ] **VPS provisioned** — Ubuntu 22.04 LTS, minimum 2 GB RAM, 40 GB SSD
- [ ] **SSH key auth configured** — password login disabled
- [ ] **Root login disabled** — using non-root user with sudo
- [ ] **Firewall active** — `ufw status` shows active, port 22 restricted to your IP
- [ ] **Docker installed** — `docker --version` returns 24.x or newer
- [ ] **Docker Compose installed** — `docker compose version` returns v2.x
- [ ] **Bot files uploaded** — files visible at `~/trading-bot/`

---

## PHASE 2 — Configuration

- [ ] **.env created** — copied from `.env.example`, all required fields filled
- [ ] **API_SECRET set** — generated with `openssl rand -hex 32`, NOT the example value
- [ ] **API_HOST=127.0.0.1** — NOT `0.0.0.0` (would expose API to internet)
- [ ] **HYPERLIQUID_PRIVATE_KEY valid** — starts with `0x`, 64 hex chars after prefix
- [ ] **HYPERLIQUID_WALLET_ADDRESS valid** — the public address associated with your agent key
- [ ] **.env permissions secured** — `ls -la .env` shows `-rw-------` (600)
- [ ] **AI API key valid** — tested independently (OpenAI/Anthropic dashboard)
- [ ] **ASSETS configured** — using valid Hyperliquid symbol names (e.g. `BTC`, `ETH`, not `BTC-USD`)
- [ ] **DRY_RUN=true** — for initial paper trading phase
- [ ] **SAFE_RETAIL_MODE=true** — always true for new deployments
- [ ] **Telegram configured** (strongly recommended) — token and chat ID set and tested

---

## PHASE 3 — Paper Trading (Minimum 2 Weeks)

**Do not skip this phase. Paper trading reveals configuration errors and strategy behavior before real money is at risk.**

- [ ] **Bot deployed** — `./scripts/deploy.sh` completed without errors
- [ ] **Bot healthy** — `./scripts/health_check.sh` shows all green
- [ ] **Startup Telegram message received** — proves alerting works before live trading
- [ ] **First cycle completed** — `data/diary.jsonl` has at least one entry
- [ ] **API endpoints responding** — `curl "http://localhost:3000/health?key=YOUR_SECRET"` returns `status: ok`
- [ ] **Paper positions opening** — bot is making decisions (check `/status`)
- [ ] **SL orders visible** — every position has a stop-loss in `/status` output
- [ ] **2 weeks paper trading completed** — strategy has been observed over multiple market conditions
- [ ] **No FAILED_NO_STOP alarms** — zero unresolved critical alarms during paper phase
- [ ] **Backups working** — `ls backups/` shows regularly timestamped directories
- [ ] **Restart tested** — ran `docker compose restart bot`, confirmed auto-reconciliation worked

---

## PHASE 4 — Pre-Live Checklist

Complete these immediately before switching to live trading:

### Account Setup
- [ ] **Hyperliquid account funded** — USDC deposited, balance confirmed
- [ ] **Agent wallet configured** — agent key approved for your main account on Hyperliquid
- [ ] **Starting capital decided** — start with an amount you can afford to lose entirely
- [ ] **Live allocation configured** — `AI_CAPITAL_PCT` and position sizes set appropriately for your capital
- [ ] **Leverage reviewed** — conservative preset uses 3x max; understand what this means for your capital

### Risk Settings
- [ ] **DAILY_LOSS_CIRCUIT_BREAKER_PCT understood** — you know at what daily loss the bot stops trading
- [ ] **MAX_POSITION_PCT understood** — maximum % of account per trade
- [ ] **MAX_LEVERAGE understood** — with conservative preset: 3x maximum
- [ ] **Mandatory SL confirmed** — `MANDATORY_SL=true` is set

### Emergency Procedures Known
- [ ] **Emergency stop command memorised** — `docker compose stop bot` (stops bot, NOT exchange positions)
- [ ] **Hyperliquid interface accessible** — you can log in and manually close positions
- [ ] **SSH access confirmed** — you can connect to the VPS from your phone if needed
- [ ] **Support contact saved** — you have the support email/Telegram for this bot

---

## PHASE 5 — Going Live

- [ ] **DRY_RUN=false** — change in `.env`
- [ ] **Re-deployed after config change** — `./scripts/deploy.sh`
- [ ] **Startup Telegram message shows LIVE TRADING** — not paper mode
- [ ] **First real position reviewed** — size matches expected allocation
- [ ] **Balance in `/status` matches Hyperliquid UI** — within normal variation

---

## PHASE 6 — Ongoing Operations

### Daily (5 minutes)
- [ ] Telegram shows no EMERGENCY alerts
- [ ] Bot is running: `docker compose ps`

### Weekly (10 minutes)
- [ ] Run `./scripts/health_check.sh`
- [ ] Review disk space: `df -h`
- [ ] Check alarm history: `/alarms` endpoint

### Monthly
- [ ] Review overall PnL from diary
- [ ] Check backup files are valid
- [ ] Review and update risk parameters if needed

---

## LEGAL REMINDER

By deploying this bot with real capital, you confirm you have:
- Read and understood the legal disclaimer provided with this software
- Accepted that this is experimental software with no profit guarantees
- Accepted that you may lose some or all of your capital
- Confirmed this is your own capital (not borrowed, not client funds)
- Confirmed you understand how to manually close positions on Hyperliquid
- Confirmed that trading perpetual futures may be restricted in your jurisdiction

**This is not financial advice. Past paper trading results do not guarantee live trading results.**
