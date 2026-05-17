"""
src/utils/alert_integrations.py

INTEGRATION GUIDE — WHERE TO ADD ALERT CALLS IN YOUR CODEBASE

This file is a reference/patch guide. It is NOT automatically applied.
For each integration point, the file, function, and exact code change is shown.

After adding these, every critical path will push a Telegram notification.
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 1. reconciliation_service.py — FAILED_NO_STOP (most critical)
# ═══════════════════════════════════════════════════════════════════════════════

PATCH_RECONCILIATION_FAILED_NO_STOP = """
# In _flatten_unprotected_position(), after the 3-retry loop exhausts:

from src.utils.telegram_notifier import alert, AlertCode

# ADD AFTER: trade.status = "failed_no_stop"
alert(
    AlertCode.FAILED_NO_STOP,
    f"POSITION {asset} HAS NO STOP LOSS — all 3 repair attempts failed.\\n"
    f"The position is LIVE, UNPROTECTED, and at full market risk.",
    asset=asset,
    action_required=(
        "1. Open Hyperliquid NOW.\\n"
        "2. Manually set a stop-loss OR close the position entirely.\\n"
        "3. Check /alarms for full event history.\\n"
        "4. Do NOT restart the bot until position is secured."
    ),
    details={
        "size": str(trade.size),
        "entry_px": str(trade.entry_price),
        "direction": trade.direction,
        "upnl": str(getattr(trade, "unrealized_pnl", "unknown")),
    },
)
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 2. reconciliation_service.py — SL repair failure (before flatten attempt)
# ═══════════════════════════════════════════════════════════════════════════════

PATCH_RECONCILIATION_SL_REPAIR_FAILED = """
# In _repair_stop_loss(), when all SL placement retries are exhausted
# (just before returning False):

from src.utils.telegram_notifier import alert, AlertCode

alert(
    AlertCode.STOP_LOSS_REPAIR_FAILED,
    f"Stop-loss repair for {asset} exhausted all retries. "
    f"Attempting emergency market flatten.",
    asset=asset,
    action_required="Monitor closely — attempting emergency close now.",
    details={"repair_attempts": "3", "direction": direction},
)
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 3. cycle_runner.py — Startup reconciliation failure
# ═══════════════════════════════════════════════════════════════════════════════

PATCH_STARTUP_RECONCILIATION_FAILED = """
# In the startup reconciliation except block:

from src.utils.telegram_notifier import alert, AlertCode

# REPLACE the current except block with:
except Exception as exc:
    logging.critical("Startup reconciliation failed: %s", exc, exc_info=True)
    alert(
        AlertCode.STARTUP_RECONCILIATION_FAILED,
        f"Bot started but could NOT reconcile with exchange.\\n"
        f"Error: {exc}\\n"
        f"Bot is retrying every 30 seconds before accepting trades.",
        action_required=(
            "Check exchange connectivity. If exchange is down, bot will "
            "retry automatically. If persists >10 minutes, restart bot."
        ),
    )
    # Then continue your retry loop as before
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 4. risk_manager.py — Circuit breaker activation
# ═══════════════════════════════════════════════════════════════════════════════

PATCH_CIRCUIT_BREAKER = """
# In _trigger_circuit_breaker() or wherever circuit_breaker_active is set True:

from src.utils.telegram_notifier import alert, AlertCode

alert(
    AlertCode.CIRCUIT_BREAKER_ACTIVATED,
    f"Daily drawdown circuit breaker ACTIVATED.\\n"
    f"Drawdown: {drawdown_pct:.2f}% (limit: {limit_pct:.2f}%)\\n"
    f"New entries BLOCKED for the rest of the trading day.",
    action_required=(
        "No immediate action needed — existing positions continue with SL.\\n"
        "Review today's losses before re-enabling."
    ),
    details={
        "drawdown_pct": f"{drawdown_pct:.2f}%",
        "limit_pct": f"{limit_pct:.2f}%",
        "balance": f"{balance:.2f}",
    },
)
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 5. risk_manager.py — High drawdown warning (before circuit breaker fires)
# ═══════════════════════════════════════════════════════════════════════════════

PATCH_HIGH_DRAWDOWN_WARNING = """
# In check_daily_drawdown(), add a warning threshold at 75% of breaker limit:
# e.g., if limit is 8%, warn at 6%

from src.utils.telegram_notifier import alert, AlertCode

WARNING_THRESHOLD = 0.75  # warn at 75% of the circuit breaker threshold

if drawdown_pct >= (limit_pct * WARNING_THRESHOLD) and not self._drawdown_warned:
    self._drawdown_warned = True
    alert(
        AlertCode.HIGH_DRAWDOWN_WARNING,
        f"Daily drawdown approaching circuit breaker limit.\\n"
        f"Current: {drawdown_pct:.2f}% | Limit: {limit_pct:.2f}%",
        action_required="Monitor closely. Reduce exposure if approaching limit.",
        details={"drawdown_pct": f"{drawdown_pct:.2f}%"},
    )
elif drawdown_pct < (limit_pct * WARNING_THRESHOLD):
    self._drawdown_warned = False  # reset when recovered
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 6. execution_service.py — Position opened/closed
# ═══════════════════════════════════════════════════════════════════════════════

PATCH_POSITION_EVENTS = """
# In _execute_intent(), after successful position open confirmation:

from src.utils.telegram_notifier import alert, AlertCode

alert(
    AlertCode.POSITION_OPENED,
    f"{'LONG' if is_buy else 'SHORT'} {asset} opened.\\n"
    f"Size: {amount:.4f} | Entry: {fill_price:.4f} | Leverage: {leverage}x\\n"
    f"SL: {sl_price:.4f} | TP: {tp_price:.4f}",
    asset=asset,
    details={
        "direction": "LONG" if is_buy else "SHORT",
        "size": f"{amount:.4f}",
        "entry": f"{fill_price:.4f}",
        "leverage": f"{leverage}x",
        "alloc_usd": f"${alloc_usd:.2f}",
    },
)

# After successful position close:
alert(
    AlertCode.POSITION_CLOSED,
    f"{'LONG' if was_long else 'SHORT'} {asset} CLOSED.\\n"
    f"PnL: {'+'if realized_pnl >= 0 else ''}{realized_pnl:.4f} USDC\\n"
    f"Reason: {close_reason}",
    asset=asset,
    details={
        "pnl": f"{realized_pnl:+.4f}",
        "close_price": f"{close_price:.4f}",
        "reason": close_reason,
    },
)
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 7. decision_pipeline.py — Repeated strategy failure
# ═══════════════════════════════════════════════════════════════════════════════

PATCH_REPEATED_STRATEGY_FAILURE = """
# In run_strategies() or wherever cycles_without_actionable_decision is checked:

from src.utils.telegram_notifier import alert, AlertCode

# After the existing CRITICAL log for consecutive empty cycles:
if cycles_without_actionable_decision >= 2:
    alert(
        AlertCode.REPEATED_AI_FAILURE,
        f"Bot has produced no actionable decisions for "
        f"{cycles_without_actionable_decision} consecutive cycles.\\n"
        f"Strategy may be stuck, AI API may be down, or market conditions "
        f"are triggering consistent holds.",
        action_required=(
            "Check AI provider API status. Check trading.log for errors. "
            "Bot is safe — holding all existing positions with SL."
        ),
        details={"consecutive_empty_cycles": str(cycles_without_actionable_decision)},
    )
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 8. hyperliquid_api.py — Exchange API failure / disconnect
# ═══════════════════════════════════════════════════════════════════════════════

PATCH_EXCHANGE_DISCONNECT = """
# In _retry(), after all retries are exhausted:

from src.utils.telegram_notifier import alert, AlertCode

alert(
    AlertCode.EXCHANGE_DISCONNECT,
    f"Exchange API call failed after {MAX_RETRIES} retries.\\n"
    f"Method: {method_name}\\nLast error: {last_error}",
    action_required=(
        "Bot will attempt reconnection automatically. "
        "If exchange is unreachable for >5 minutes, manually check Hyperliquid status."
    ),
    details={"method": method_name, "error": str(last_error)[:100]},
)
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 9. main.py / cycle_runner.py — Bot startup
# ═══════════════════════════════════════════════════════════════════════════════

PATCH_BOT_STARTUP = """
# In main() or run() at startup, AFTER notifier.start():

from src.utils.telegram_notifier import alert, AlertCode
import os

alert(
    AlertCode.BOT_STARTED,
    f"Bot is online and initialising.\\n"
    f"Assets: {', '.join(settings.assets)}\\n"
    f"Mode: {'DRY RUN' if settings.dry_run else 'LIVE TRADING'}\\n"
    f"Safe mode: {settings.safe_retail_mode}",
    details={
        "mode": "DRY_RUN" if settings.dry_run else "LIVE",
        "assets": ",".join(settings.assets),
        "preset": getattr(settings, "safe_retail_preset", "custom"),
    },
)
"""

# ═══════════════════════════════════════════════════════════════════════════════
# 10. cycle_runner.py — Unexpected exception handler
# ═══════════════════════════════════════════════════════════════════════════════

PATCH_UNEXPECTED_EXCEPTION = """
# Wrap the main cycle loop body in a broad exception handler:

from src.utils.telegram_notifier import alert, AlertCode
import traceback

try:
    await self._run_cycle()
except Exception as exc:
    logging.critical("Unexpected cycle exception: %s", exc, exc_info=True)
    alert(
        AlertCode.UNEXPECTED_EXCEPTION,
        f"Unexpected exception in trading cycle:\\n{type(exc).__name__}: {exc}",
        action_required=(
            "Check trading.log immediately. Bot will retry next cycle. "
            "If recurring, restart bot and investigate."
        ),
        details={"exception_type": type(exc).__name__, "error": str(exc)[:200]},
    )
    # Continue to next cycle — do not crash
"""
