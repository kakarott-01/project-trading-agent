"""
src/utils/telegram_notifier.py

Production-grade Telegram alerting service.

Features:
  - Non-blocking fire-and-forget (never blocks the trading loop)
  - Async delivery queue (background worker)
  - Retry with exponential backoff (3 attempts)
  - Rate limiting (18 messages/minute, under Telegram's 20/min hard limit)
  - Message deduplication (suppresses identical alerts within 60 seconds)
  - Emergency bypass (EMERGENCY severity skips dedup and rate limit)
  - Graceful failure (never raises, never crashes the bot)

Usage:
    # At startup in main.py / cycle_runner.py:
    from src.utils.telegram_notifier import init_telegram_notifier, alert, AlertCode
    notifier = init_telegram_notifier(settings)
    await notifier.start()

    # Anywhere in the codebase (sync or async):
    alert(AlertCode.FAILED_NO_STOP,
          f"BTC has no stop loss — position is unprotected",
          asset="BTC",
          action_required="Log into exchange and manually close or set SL")

    # At shutdown:
    await notifier.stop()

Integration points (search codebase for these and add alert() calls):
  - reconciliation_service.py: _flatten_unprotected_position failure
  - reconciliation_service.py: _repair_stop_loss exhausted retries
  - execution_service.py: order submission failure
  - risk_manager.py: circuit breaker activation
  - risk_manager.py: high drawdown warning
  - cycle_runner.py: startup reconciliation failure
  - decision_pipeline.py: repeated strategy failure
  - hyperliquid_api.py: exchange disconnect / API failure
"""

import asyncio
import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Severity and code definitions
# ─────────────────────────────────────────────────────────────────────────────

class AlertSeverity(str, Enum):
    INFO      = "INFO"
    WARNING   = "WARNING"
    CRITICAL  = "CRITICAL"
    EMERGENCY = "EMERGENCY"


class AlertCode(str, Enum):
    # System lifecycle
    BOT_STARTED                   = "BOT_STARTED"
    BOT_RESTARTED                 = "BOT_RESTARTED"
    BOT_SHUTDOWN                  = "BOT_SHUTDOWN"
    DOCKER_UNHEALTHY              = "DOCKER_UNHEALTHY"
    VPS_STARTUP                   = "VPS_STARTUP"

    # Exchange connectivity
    EXCHANGE_DISCONNECT           = "EXCHANGE_DISCONNECT"
    EXCHANGE_RECONNECT            = "EXCHANGE_RECONNECT"
    API_FAILURE                   = "API_FAILURE"
    API_THROTTLED                 = "API_THROTTLED"

    # Reconciliation
    STARTUP_RECONCILIATION_FAILED = "STARTUP_RECONCILIATION_FAILED"
    RECONCILIATION_STALE          = "RECONCILIATION_STALE"

    # Trading events
    POSITION_OPENED               = "POSITION_OPENED"
    POSITION_CLOSED               = "POSITION_CLOSED"
    ORDER_REJECTED                = "ORDER_REJECTED"
    PARTIAL_FILL                  = "PARTIAL_FILL"

    # Risk & safety — highest priority
    FAILED_NO_STOP                = "FAILED_NO_STOP"
    STOP_LOSS_REPAIR_FAILED       = "STOP_LOSS_REPAIR_FAILED"
    CIRCUIT_BREAKER_ACTIVATED     = "CIRCUIT_BREAKER_ACTIVATED"
    HIGH_DRAWDOWN_WARNING         = "HIGH_DRAWDOWN_WARNING"
    LIQUIDATION_DANGER            = "LIQUIDATION_DANGER"
    CRITICAL_RISK_EVENT           = "CRITICAL_RISK_EVENT"
    FORCE_CLOSE_FAILED            = "FORCE_CLOSE_FAILED"

    # Strategy / AI
    STRATEGY_FAILURE              = "STRATEGY_FAILURE"
    REPEATED_AI_FAILURE           = "REPEATED_AI_FAILURE"
    REPEATED_STRATEGY_FAILURE     = "REPEATED_STRATEGY_FAILURE"

    # Manual intervention
    MANUAL_INTERVENTION_REQUIRED  = "MANUAL_INTERVENTION_REQUIRED"
    UNEXPECTED_EXCEPTION          = "UNEXPECTED_EXCEPTION"


# Default severity map — EMERGENCY bypasses dedup and rate limit
_CODE_SEVERITY: dict[AlertCode, AlertSeverity] = {
    AlertCode.BOT_STARTED:                   AlertSeverity.INFO,
    AlertCode.BOT_RESTARTED:                 AlertSeverity.WARNING,
    AlertCode.BOT_SHUTDOWN:                  AlertSeverity.INFO,
    AlertCode.DOCKER_UNHEALTHY:              AlertSeverity.CRITICAL,
    AlertCode.VPS_STARTUP:                   AlertSeverity.INFO,

    AlertCode.EXCHANGE_DISCONNECT:           AlertSeverity.CRITICAL,
    AlertCode.EXCHANGE_RECONNECT:            AlertSeverity.INFO,
    AlertCode.API_FAILURE:                   AlertSeverity.WARNING,
    AlertCode.API_THROTTLED:                 AlertSeverity.WARNING,

    AlertCode.STARTUP_RECONCILIATION_FAILED: AlertSeverity.CRITICAL,
    AlertCode.RECONCILIATION_STALE:          AlertSeverity.WARNING,

    AlertCode.POSITION_OPENED:               AlertSeverity.INFO,
    AlertCode.POSITION_CLOSED:               AlertSeverity.INFO,
    AlertCode.ORDER_REJECTED:                AlertSeverity.WARNING,
    AlertCode.PARTIAL_FILL:                  AlertSeverity.WARNING,

    AlertCode.FAILED_NO_STOP:               AlertSeverity.EMERGENCY,
    AlertCode.STOP_LOSS_REPAIR_FAILED:       AlertSeverity.EMERGENCY,
    AlertCode.CIRCUIT_BREAKER_ACTIVATED:     AlertSeverity.CRITICAL,
    AlertCode.HIGH_DRAWDOWN_WARNING:         AlertSeverity.WARNING,
    AlertCode.LIQUIDATION_DANGER:            AlertSeverity.EMERGENCY,
    AlertCode.CRITICAL_RISK_EVENT:           AlertSeverity.EMERGENCY,
    AlertCode.FORCE_CLOSE_FAILED:            AlertSeverity.EMERGENCY,

    AlertCode.STRATEGY_FAILURE:              AlertSeverity.WARNING,
    AlertCode.REPEATED_AI_FAILURE:           AlertSeverity.CRITICAL,
    AlertCode.REPEATED_STRATEGY_FAILURE:     AlertSeverity.CRITICAL,

    AlertCode.MANUAL_INTERVENTION_REQUIRED:  AlertSeverity.EMERGENCY,
    AlertCode.UNEXPECTED_EXCEPTION:          AlertSeverity.CRITICAL,
}

_SEVERITY_EMOJI: dict[AlertSeverity, str] = {
    AlertSeverity.INFO:      "ℹ️",
    AlertSeverity.WARNING:   "⚠️",
    AlertSeverity.CRITICAL:  "🚨",
    AlertSeverity.EMERGENCY: "🆘",
}


# ─────────────────────────────────────────────────────────────────────────────
# Alert dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Alert:
    code: AlertCode
    message: str
    asset: Optional[str] = None
    severity: Optional[AlertSeverity] = None
    action_required: Optional[str] = None
    details: Optional[dict] = field(default_factory=dict)

    def __post_init__(self):
        if self.severity is None:
            self.severity = _CODE_SEVERITY.get(self.code, AlertSeverity.INFO)


# ─────────────────────────────────────────────────────────────────────────────
# TelegramNotifier
# ─────────────────────────────────────────────────────────────────────────────

class TelegramNotifier:
    """
    Async-safe Telegram alert delivery service.

    Thread-safety: send() and send_async() are safe to call from any context.
    The background worker runs in the bot's main asyncio event loop.
    """

    _SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"

    # Tuning
    MAX_RETRIES       = 3
    RETRY_BASE_DELAY  = 2.0    # seconds; doubles each attempt
    RATE_WINDOW       = 60     # seconds
    RATE_MAX_MSGS     = 18     # per RATE_WINDOW (Telegram limit is 20; leave buffer)
    DEDUP_WINDOW      = 60     # seconds; suppress identical alert within this window
    QUEUE_MAX         = 200    # drop alerts if queue is full

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        enabled: bool = True,
        bot_name: str = "TradingBot",
    ):
        self.bot_token  = bot_token
        self.chat_id    = chat_id
        self.enabled    = enabled
        self.bot_name   = bot_name

        # Rate limiting
        self._rate_deque: deque[float] = deque()

        # Dedup: hash → monotonic timestamp of last send
        self._sent_hashes: dict[str, float] = {}

        # Delivery queue
        self._queue: asyncio.Queue[Alert] = asyncio.Queue(maxsize=self.QUEUE_MAX)
        self._worker_task: Optional[asyncio.Task] = None

        # Stats (read-only from outside)
        self.stats = {
            "sent": 0,
            "dropped_dedup": 0,
            "dropped_queue_full": 0,
            "failed": 0,
        }

        log.info(
            "TelegramNotifier init — enabled=%s bot=%s",
            enabled, bot_name,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start background delivery worker. Call once at bot startup."""
        if not self.enabled:
            log.info("TelegramNotifier disabled — no token/chat_id configured.")
            return
        if self._worker_task and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(
            self._delivery_worker(), name="telegram_delivery"
        )
        log.info("TelegramNotifier worker started.")

    async def stop(self) -> None:
        """Drain queue and stop. Call at bot shutdown."""
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        log.info(
            "TelegramNotifier stopped. Stats: %s", self.stats
        )

    # ── Public send API ───────────────────────────────────────────────────────

    def send(self, alert: Alert) -> None:
        """
        Fire-and-forget from any context (sync or async, any thread).
        Never raises. Drops silently if queue is full.
        """
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(alert)
        except asyncio.QueueFull:
            self.stats["dropped_queue_full"] += 1
            log.warning("Telegram queue full — dropped: %s", alert.code)

    async def send_async(self, alert: Alert) -> None:
        """Async variant — waits up to 0.5s for queue space, then drops."""
        if not self.enabled:
            return
        try:
            await asyncio.wait_for(self._queue.put(alert), timeout=0.5)
        except (asyncio.QueueFull, asyncio.TimeoutError):
            self.stats["dropped_queue_full"] += 1
            log.warning("Telegram queue full (async) — dropped: %s", alert.code)

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _delivery_worker(self) -> None:
        """Runs forever, draining the queue and delivering messages."""
        while True:
            try:
                alert = await self._queue.get()
                await self._process(alert)
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("Telegram delivery worker error: %s", exc, exc_info=True)

    async def _process(self, alert: Alert) -> None:
        is_emergency = alert.severity == AlertSeverity.EMERGENCY

        # Deduplication (bypass for EMERGENCY)
        if not is_emergency:
            key = self._hash(alert)
            now = time.monotonic()
            last = self._sent_hashes.get(key, 0.0)
            if now - last < self.DEDUP_WINDOW:
                self.stats["dropped_dedup"] += 1
                log.debug("Telegram dedup — suppressed: %s", alert.code)
                return

        # Rate limit (bypass for EMERGENCY)
        if not is_emergency:
            await self._enforce_rate_limit()

        text = self._format(alert)
        ok = await self._send_with_retry(text)

        if ok:
            self.stats["sent"] += 1
            now = time.monotonic()
            self._sent_hashes[self._hash(alert)] = now
            self._rate_deque.append(now)
            # Prune old dedup entries
            cutoff = now - self.DEDUP_WINDOW * 2
            self._sent_hashes = {
                k: v for k, v in self._sent_hashes.items() if v > cutoff
            }
        else:
            self.stats["failed"] += 1
            log.error("Telegram delivery failed after retries: %s", alert.code)

    def _hash(self, alert: Alert) -> str:
        raw = f"{alert.code}:{alert.asset}:{alert.message[:120]}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    async def _enforce_rate_limit(self) -> None:
        while True:
            now = time.monotonic()
            cutoff = now - self.RATE_WINDOW
            while self._rate_deque and self._rate_deque[0] < cutoff:
                self._rate_deque.popleft()
            if len(self._rate_deque) < self.RATE_MAX_MSGS:
                return
            sleep_for = self._rate_deque[0] + self.RATE_WINDOW - now + 0.1
            log.debug("Telegram rate limit — sleeping %.1fs", sleep_for)
            await asyncio.sleep(max(sleep_for, 0.1))

    async def _send_with_retry(self, text: str) -> bool:
        import aiohttp

        url = self._SEND_URL.format(token=self.bot_token)
        payload = {
            "chat_id":                  self.chat_id,
            "text":                     text,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        }

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as session:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            return True
                        if resp.status == 429:
                            retry_after = int(resp.headers.get("Retry-After", 10))
                            log.warning(
                                "Telegram 429 — waiting %ds (attempt %d)",
                                retry_after, attempt,
                            )
                            await asyncio.sleep(retry_after)
                            continue
                        body = await resp.text()
                        log.warning(
                            "Telegram HTTP %d (attempt %d): %s",
                            resp.status, attempt, body[:200],
                        )
            except Exception as exc:
                log.warning("Telegram send attempt %d failed: %s", attempt, exc)

            if attempt < self.MAX_RETRIES:
                await asyncio.sleep(self.RETRY_BASE_DELAY ** attempt)

        return False

    def _format(self, alert: Alert) -> str:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        emoji   = _SEVERITY_EMOJI.get(alert.severity, "📢")
        sev     = alert.severity.value if alert.severity else "INFO"

        lines = [
            f"{emoji} <b>[{sev}] {alert.code.value}</b>",
            f"🤖 <i>{self.bot_name}</i>  |  🕐 {now_str}",
        ]

        if alert.asset:
            lines.append(f"📊 Asset: <b>{alert.asset}</b>")

        lines.append("")
        lines.append(alert.message)

        if alert.action_required:
            lines.append("")
            lines.append(f"⚡ <b>ACTION REQUIRED:</b>")
            lines.append(f"→ {alert.action_required}")

        if alert.details:
            pairs = "  ".join(f"{k}={v}" for k, v in alert.details.items())
            if pairs:
                lines.append("")
                lines.append(f"<code>{pairs}</code>")

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton + convenience API
# ─────────────────────────────────────────────────────────────────────────────

_notifier: Optional[TelegramNotifier] = None


def init_telegram_notifier(settings) -> TelegramNotifier:
    """
    Initialise the global notifier from application settings.
    Call ONCE at startup, before starting the asyncio event loop.

    Settings object must expose:
        telegram_bot_token: str | None
        telegram_chat_id:   str | None
        bot_name:           str  (optional, default "TradingBot")
    """
    global _notifier
    token   = getattr(settings, "telegram_bot_token", None) or ""
    chat_id = getattr(settings, "telegram_chat_id",   None) or ""
    enabled = bool(token and chat_id)

    if not enabled:
        log.warning(
            "Telegram alerting DISABLED — set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID in .env to enable push notifications."
        )

    _notifier = TelegramNotifier(
        bot_token = token,
        chat_id   = chat_id,
        enabled   = enabled,
        bot_name  = getattr(settings, "bot_name", "HyperliquidBot"),
    )
    return _notifier


def get_notifier() -> Optional[TelegramNotifier]:
    return _notifier


def alert(
    code: AlertCode,
    message: str,
    asset: Optional[str] = None,
    severity: Optional[AlertSeverity] = None,
    action_required: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """
    Convenience function — fire-and-forget from ANYWHERE in the codebase.
    Safe to call from sync code, async code, and threads.
    Never raises.

    Example — in reconciliation_service.py after failed flatten:
        from src.utils.telegram_notifier import alert, AlertCode
        alert(
            AlertCode.FAILED_NO_STOP,
            f"Position {asset} has no stop loss after 3 repair attempts. "
            f"Position remains LIVE and UNPROTECTED.",
            asset=asset,
            action_required=(
                "1. Log into Hyperliquid immediately.\n"
                "2. Manually set a stop-loss OR close the position.\n"
                "3. Check /alarms endpoint for full detail."
            ),
            details={"size": size, "entry_px": entry_px, "unrealized_pnl": upnl},
        )
    """
    global _notifier
    if _notifier is None:
        log.debug("Notifier not initialised — dropped: %s", code)
        return
    _notifier.send(Alert(
        code=code,
        message=message,
        asset=asset,
        severity=severity,
        action_required=action_required,
        details=details or {},
    ))
