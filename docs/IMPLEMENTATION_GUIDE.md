# IMPLEMENTATION GUIDE
## Complete Instructions: Where Every File Goes and What to Do

This is your master reference. Read it completely before touching any files.
Follow the order exactly — some steps depend on earlier ones.

---

## YOUR EXISTING PROJECT STRUCTURE

Assuming your project looks like this (adjust if different):
```
your-trading-bot/          ← your existing project root
├── src/
│   ├── agent/
│   │   └── decision_maker.py
│   ├── application/
│   │   ├── cycle_runner.py
│   │   ├── execution_service.py
│   │   └── reconciliation_service.py
│   ├── config/
│   │   └── settings.py
│   ├── exchanges/
│   │   └── dry_run.py
│   ├── interfaces/
│   │   └── api_server.py
│   ├── risk_manager.py
│   ├── strategies/
│   │   └── ai_strategy.py
│   └── utils/
│       └── (existing utils)
├── algo.py
├── Dockerfile
├── pyproject.toml
├── .env.example
├── README.md
└── (other files)
```

---

## STEP 1 — ADD NEW FILES (No Existing Code Changed)

These files are pure additions. Drop them in directly.

### 1a. Telegram Notifier Service
```
COPY:  src/utils/telegram_notifier.py
TO:    your-trading-bot/src/utils/telegram_notifier.py
```
This is the complete Telegram alerting service. Self-contained, no dependencies on your existing code.

### 1b. Backup Worker
```
COPY:  scripts/backup_worker.py
TO:    your-trading-bot/scripts/backup_worker.py
```
Runs as a Docker sidecar. No changes needed.

### 1c. Logrotate Worker
```
COPY:  scripts/logrotate_worker.py
TO:    your-trading-bot/scripts/logrotate_worker.py
```
Runs as a Docker sidecar. No changes needed.

### 1d. Deploy Script
```
COPY:  scripts/deploy.sh
TO:    your-trading-bot/scripts/deploy.sh
THEN:  chmod +x your-trading-bot/scripts/deploy.sh
```

### 1e. Backup Script
```
COPY:  scripts/backup.sh
TO:    your-trading-bot/scripts/backup.sh
THEN:  chmod +x your-trading-bot/scripts/backup.sh
```

### 1f. Ops Scripts (restore, update, health check)
The ops_scripts.sh file contains 3 separate scripts joined by `---` separators.
Split them into 3 files:

```
COPY lines 1–44 (restore.sh) TO:        your-trading-bot/scripts/restore.sh
COPY lines 47–91 (update.sh) TO:        your-trading-bot/scripts/update.sh
COPY lines 93–end (health_check.sh) TO: your-trading-bot/scripts/health_check.sh

THEN:
chmod +x your-trading-bot/scripts/restore.sh
chmod +x your-trading-bot/scripts/update.sh
chmod +x your-trading-bot/scripts/health_check.sh
```

### 1g. Monitoring Dashboard
```
COPY:  monitoring/dashboard.html
TO:    your-trading-bot/monitoring/dashboard.html
```
This is a standalone HTML file. Open in any browser via SSH tunnel.

### 1h. Docker Compose
```
COPY:  docker-compose.yml
TO:    your-trading-bot/docker-compose.yml
```
If you already have a docker-compose.yml, REPLACE it with this one.
This version adds the backup and logrotate sidecars.

### 1i. Environment Template
```
COPY:  .env.example
TO:    your-trading-bot/.env.example
```
Adds TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, BOT_NAME, and other new fields.

### 1j. Documentation Files
```
COPY:  docs/production_deployment.md  →  your-trading-bot/docs/production_deployment.md
COPY:  docs/telegram_alerts.md        →  your-trading-bot/docs/telegram_alerts.md
COPY:  docs/monitoring.md             →  your-trading-bot/docs/monitoring.md
COPY:  docs/backup_recovery.md        →  your-trading-bot/docs/backup_recovery.md
COPY:  docs/onboarding_checklist.md   →  your-trading-bot/docs/onboarding_checklist.md
COPY:  docs/troubleshooting.md        →  your-trading-bot/docs/troubleshooting.md
COPY:  docs/final_audit_and_commercial_review.md → your-trading-bot/docs/final_audit_and_commercial_review.md
```

---

## STEP 2 — MODIFY EXISTING FILES

These require you to edit your existing code. Each change is described precisely.

### 2a. Fix DryRunBroker (NEW-001 — High Priority)

**File to edit**: `your-trading-bot/src/exchanges/dry_run.py`

Open `dry_run_margin_patch.py` and apply the three patches:

**Patch A — `_open_market` method:**
Find your existing `_open_market` method. Replace its body with the code in `AFTER_OPEN_MARKET`.
Key change: Add margin calculation and deduction from `self.state["cash"]`.

**Patch B — `_close_position` method:**
Find your existing `_close_position` method. Replace its body with the code in `AFTER_CLOSE_POSITION`.
Key change: Return margin_posted + realized_pnl to cash (not just pnl).

**Patch C — `get_user_state` method:**
Find your existing `get_user_state` method. Replace it with `AFTER_GET_USER_STATE`.
Key change: account_value = free_cash + posted_margin + unrealized_pnl.

After applying: run the tests in `TEST_MARGIN_ACCOUNTING` to verify.

---

### 2b. Add Telegram Fields to Settings

**File to edit**: `your-trading-bot/src/config/settings.py`

Open `src/config/settings_patch.py` and copy the `SETTINGS_ADDITIONS` block.

Add these fields to your `Settings` class (inside the class body, near other optional fields):
```python
# ── Telegram Alerting ──────────────────────────────────────────────────────
telegram_bot_token: Optional[str] = Field(default=None, env="TELEGRAM_BOT_TOKEN")
telegram_chat_id: Optional[str]   = Field(default=None, env="TELEGRAM_CHAT_ID")
bot_name: str                     = Field(default="HyperliquidBot", env="BOT_NAME")

# ── Operational ────────────────────────────────────────────────────────────
bot_version: str       = Field(default="1.0.0", env="BOT_VERSION")
data_dir: str          = Field(default="./data", env="DATA_DIR")
log_max_size_mb: int   = Field(default=50, env="LOG_MAX_SIZE_MB")
```

Make sure `Optional` is imported: `from typing import Optional`

---

### 2c. Initialise Telegram at Bot Startup

**File to edit**: Your main entry point (likely `main.py`, `bot.py`, or wherever `CycleRunner` is created and started)

Add at the top of the file:
```python
from src.utils.telegram_notifier import init_telegram_notifier, alert, AlertCode
```

In your `main()` or `async def run()` function, ADD THESE LINES before starting the cycle runner:
```python
# Initialise Telegram (do this FIRST, before anything else)
notifier = init_telegram_notifier(settings)
await notifier.start()

# Send startup notification
alert(
    AlertCode.BOT_STARTED,
    f"Bot online. Assets: {', '.join(settings.assets)} | "
    f"Mode: {'DRY RUN' if settings.dry_run else 'LIVE TRADING'}",
    details={"version": getattr(settings, 'bot_version', '?')},
)
```

At the very end, in your cleanup/shutdown section:
```python
await notifier.stop()
```

---

### 2d. Add FAILED_NO_STOP Telegram Alert

**File to edit**: `your-trading-bot/src/application/reconciliation_service.py`

Add at the top of the file:
```python
from src.utils.telegram_notifier import alert, AlertCode
```

Find the `_flatten_unprotected_position` method. Find where `trade.status = "failed_no_stop"` is set. IMMEDIATELY AFTER that line, add:
```python
alert(
    AlertCode.FAILED_NO_STOP,
    f"POSITION {asset} HAS NO STOP LOSS — all repair and flatten attempts failed.\n"
    f"Position is LIVE and UNPROTECTED.",
    asset=asset,
    action_required=(
        "1. Open Hyperliquid NOW.\n"
        "2. Manually set a stop-loss OR close the position entirely.\n"
        "3. Check /alarms for full detail."
    ),
    details={
        "size": str(getattr(trade, "size", "?")),
        "entry_px": str(getattr(trade, "entry_price", "?")),
        "direction": getattr(trade, "direction", "?"),
    },
)
```

Also find `_repair_stop_loss` and add before returning `False` after retries:
```python
alert(
    AlertCode.STOP_LOSS_REPAIR_FAILED,
    f"Stop-loss repair for {asset} exhausted all retries. Attempting market close.",
    asset=asset,
    action_required="Emergency flatten in progress — monitor closely.",
)
```

---

### 2e. Add Circuit Breaker Alert

**File to edit**: `your-trading-bot/src/risk_manager.py`

Add at the top:
```python
from src.utils.telegram_notifier import alert, AlertCode
```

Find where `self.circuit_breaker_active = True` is set. Wrap the transition:
```python
if not self.circuit_breaker_active:
    self.circuit_breaker_active = True
    alert(
        AlertCode.CIRCUIT_BREAKER_ACTIVATED,
        f"Daily circuit breaker ACTIVATED.\n"
        f"Drawdown: {drawdown_pct:.2f}% reached limit: {self.daily_loss_circuit_breaker_pct:.1f}%\n"
        f"New entries BLOCKED for today.",
        action_required="No action needed — bot is safe. Review losses.",
        details={
            "drawdown_pct": f"{drawdown_pct:.2f}%",
            "limit_pct": f"{self.daily_loss_circuit_breaker_pct:.1f}%",
        },
    )
```

---

### 2f. Add Position Open/Close Alerts

**File to edit**: `your-trading-bot/src/application/execution_service.py`

Add at top:
```python
from src.utils.telegram_notifier import alert, AlertCode
```

After a confirmed position OPEN (after fill confirmation / reconciliation confirms position is open):
```python
alert(
    AlertCode.POSITION_OPENED,
    f"{'LONG' if is_buy else 'SHORT'} {asset}: "
    f"Entry={fill_price:.4f} Size={amount:.4f} Lev={leverage}x "
    f"SL={sl_price:.4f}",
    asset=asset,
)
```

After a confirmed position CLOSE:
```python
alert(
    AlertCode.POSITION_CLOSED,
    f"{'LONG' if was_long else 'SHORT'} {asset} CLOSED. "
    f"PnL: {realized_pnl:+.4f} USDC. Reason: {close_reason}",
    asset=asset,
    details={"pnl": f"{realized_pnl:+.4f}", "reason": close_reason},
)
```

---

### 2g. Add Repeated Strategy Failure Alert

**File to edit**: `your-trading-bot/src/application/cycle_runner.py` (or wherever `cycles_without_actionable_decision` is checked)

Add at top:
```python
from src.utils.telegram_notifier import alert, AlertCode
```

Find the block after the existing CRITICAL log for consecutive empty cycles. Add:
```python
if self.cycles_without_actionable_decision >= 2:
    alert(
        AlertCode.REPEATED_AI_FAILURE,
        f"No actionable decisions for {self.cycles_without_actionable_decision} consecutive cycles.",
        action_required="Check AI API status. Bot is safely holding positions.",
        details={"consecutive_empty": str(self.cycles_without_actionable_decision)},
    )
```

Also wrap your main cycle body with:
```python
try:
    await self._run_single_cycle()  # or whatever your main cycle method is
except Exception as exc:
    logging.critical("Unexpected cycle exception: %s", exc, exc_info=True)
    alert(
        AlertCode.UNEXPECTED_EXCEPTION,
        f"Unexpected exception: {type(exc).__name__}: {str(exc)[:200]}",
        action_required="Check trading.log. Bot will retry next cycle.",
    )
    # Do NOT re-raise — let the cycle continue
```

---

### 2h. Add Startup Reconciliation Failure Alert

**File to edit**: `your-trading-bot/src/application/cycle_runner.py` (startup section)

In the startup reconciliation except block, add:
```python
from src.utils.telegram_notifier import alert, AlertCode

except Exception as exc:
    logging.critical("Startup reconciliation failed: %s", exc, exc_info=True)
    alert(
        AlertCode.STARTUP_RECONCILIATION_FAILED,
        f"Bot started but CANNOT reconcile with exchange.\nError: {exc}\n"
        f"Retrying every 30s before accepting trades.",
        action_required="Check exchange connectivity. Bot will retry automatically.",
    )
    # continue your existing retry loop
```

---

### 2i. Update .env File

Add these new variables to your actual `.env` (not just `.env.example`):
```dotenv
# Telegram
TELEGRAM_BOT_TOKEN=          # Your bot token from @BotFather
TELEGRAM_CHAT_ID=            # Your chat ID
BOT_NAME=HyperliquidBot      # Shown in every alert

# Operational
BOT_VERSION=1.0.0
BACKUP_INTERVAL_SECONDS=300
BACKUP_RETAIN_COUNT=144
RCLONE_REMOTE=               # Optional: remote backup
LOG_MAX_SIZE_MB=50
```

---

### 2j. Add aiohttp to Dependencies

The Telegram notifier uses `aiohttp`. Add it to your `pyproject.toml`:

```toml
[tool.poetry.dependencies]
aiohttp = ">=3.9.0,<4.0.0"
```

Then run:
```bash
poetry add aiohttp
# or if using pip:
pip install "aiohttp>=3.9.0,<4.0.0"
```

---

## STEP 3 — VERIFY THE DRY RUN PATHS EXIST IN docker-compose.yml

Open the new `docker-compose.yml` and verify:

1. The `DATA_DIR` path in the `volumes` section matches your actual data directory
2. `./algo.py:/app/algo.py:ro` — algo.py exists at your project root
3. `./logs:/app/logs` — create the `logs/` directory: `mkdir -p logs`
4. `./data:/app/data` — create the `data/` directory: `mkdir -p data`
5. `./backups:/backups` — create the `backups/` directory: `mkdir -p backups`

---

## STEP 4 — CREATE REQUIRED DIRECTORIES

```bash
cd your-trading-bot
mkdir -p data logs backups scripts docs monitoring
chmod 700 data
```

---

## STEP 5 — TEST TELEGRAM BEFORE LIVE TRADING

```bash
# Quick test from Python
cd your-trading-bot
python3 -c "
import asyncio
from src.config.settings import get_settings
from src.utils.telegram_notifier import init_telegram_notifier, alert, AlertCode

async def test():
    settings = get_settings()
    notifier = init_telegram_notifier(settings)
    await notifier.start()
    alert(AlertCode.BOT_STARTED, 'Telegram test successful! Bot alerting is working.')
    await asyncio.sleep(5)  # wait for delivery
    await notifier.stop()

asyncio.run(test())
"
```

You should receive a Telegram message within 5 seconds.

---

## STEP 6 — DEPLOY

```bash
cd your-trading-bot
./scripts/deploy.sh
```

If the deploy script finds issues, fix them before proceeding.

---

## STEP 7 — VERIFY EVERYTHING WORKS

```bash
# Health check
./scripts/health_check.sh

# Confirm backup is running
docker compose ps backup
ls backups/

# Confirm logrotate is running
docker compose ps logrotate

# Check bot is cycling
docker compose logs -f bot | grep -E "cycle|decision|reconcil"
```

---

## COMPLETE FILE MAP

```
your-trading-bot/
├── src/
│   ├── config/
│   │   └── settings.py              ← MODIFY: add Telegram + operational fields
│   ├── exchanges/
│   │   └── dry_run.py               ← MODIFY: apply margin accounting patch
│   ├── application/
│   │   ├── cycle_runner.py          ← MODIFY: add alerts + exception wrapper
│   │   ├── execution_service.py     ← MODIFY: add position open/close alerts
│   │   └── reconciliation_service.py ← MODIFY: add FAILED_NO_STOP alert
│   ├── risk_manager.py              ← MODIFY: add circuit breaker alert
│   └── utils/
│       └── telegram_notifier.py     ← NEW: complete Telegram service
│
├── main.py (or your entry point)    ← MODIFY: init notifier, startup alert
│
├── scripts/
│   ├── backup_worker.py             ← NEW: Docker sidecar
│   ├── logrotate_worker.py          ← NEW: Docker sidecar
│   ├── deploy.sh                    ← NEW: one-command deploy
│   ├── backup.sh                    ← NEW: manual backup
│   ├── restore.sh                   ← NEW: restore from backup
│   ├── update.sh                    ← NEW: safe update
│   └── health_check.sh              ← NEW: health report
│
├── monitoring/
│   └── dashboard.html               ← NEW: browser monitoring dashboard
│
├── docker-compose.yml               ← REPLACE: adds backup + logrotate sidecars
├── .env.example                     ← REPLACE: adds Telegram + operational fields
├── .env                             ← MODIFY: add Telegram + operational fields
│
└── docs/
    ├── production_deployment.md     ← NEW
    ├── telegram_alerts.md           ← NEW
    ├── monitoring.md                ← NEW
    ├── backup_recovery.md           ← NEW
    ├── onboarding_checklist.md      ← NEW
    ├── troubleshooting.md           ← NEW
    └── final_audit_and_commercial_review.md ← NEW
```

---

## COMMON MISTAKES TO AVOID

1. **Don't copy `telegram_notifier.py` into the root** — it must be in `src/utils/`
2. **Don't skip the `aiohttp` dependency** — the notifier won't work without it
3. **Don't forget `await notifier.start()`** — the queue worker won't run without it
4. **Don't call `await notifier.stop()`** before all alerts have delivered — add `asyncio.sleep(3)` before stop in shutdown
5. **Don't set `API_HOST=0.0.0.0`** in the new docker-compose — it's set to `127.0.0.1`
6. **Don't skip the DryRun patch** — paper trading results will be misleading without it
7. **Don't forget `chmod +x scripts/*.sh`** — scripts won't execute without it
8. **Don't forget `mkdir -p data logs backups`** — Docker volume bind mounts need the directories to exist
