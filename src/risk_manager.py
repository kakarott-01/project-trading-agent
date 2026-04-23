"""Centralized risk management for the trading agent.

All safety guards are enforced here, independent of LLM decisions.
The LLM cannot override these limits — they are hard-coded checks
applied before every trade execution.

Changes from original:
- enforce_stop_loss validates SL is on the CORRECT side of entry
- enforce_take_profit validates TP is on the CORRECT side of entry
- Circuit breaker state persisted across restarts via risk_state.json
- Leverage check uses correct semantics (position notional / account equity)
- check_losing_positions handles both position dict formats
"""

import logging
from datetime import datetime, timezone
from typing import Any

from src.config import Settings, get_settings
from src.utils.state_persistence import load_risk_state, save_risk_state


class RiskManager:
    """Enforces risk limits on every trade before execution."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.max_position_pct = self.settings.risk.max_position_pct
        self.max_loss_per_position_pct = self.settings.risk.max_loss_per_position_pct
        self.max_leverage = self.settings.risk.max_leverage
        self.min_trade_confidence = self.settings.risk.min_trade_confidence
        self.max_total_exposure_pct = self.settings.risk.max_total_exposure_pct
        self.daily_loss_circuit_breaker_pct = (
            self.settings.risk.daily_loss_circuit_breaker_pct
        )
        self.mandatory_sl_pct = self.settings.risk.mandatory_sl_pct
        self.max_concurrent_positions = self.settings.risk.max_concurrent_positions
        self.min_balance_reserve_pct = self.settings.risk.min_balance_reserve_pct

        # Daily tracking — load persisted state so restarts don't reset the breaker
        self.circuit_breaker_active = False
        self.daily_high_value: float | None = None
        self.daily_high_date = None
        self.circuit_breaker_date = None
        self._load_persisted_state()

    def _load_persisted_state(self) -> None:
        state = load_risk_state()
        if state:
            self.circuit_breaker_active = bool(state.get("circuit_breaker_active", False))
            self.daily_high_value = state.get("daily_high_value")
            today = datetime.now(timezone.utc).date()
            self.daily_high_date = today
            if self.circuit_breaker_active:
                logging.warning(
                    "RISK: Circuit breaker WAS active when bot last stopped. "
                    "It remains active for today (%s).",
                    today,
                )

    def _persist_state(self) -> None:
        save_risk_state({
            "date": datetime.now(timezone.utc).date().isoformat(),
            "circuit_breaker_active": self.circuit_breaker_active,
            "daily_high_value": self.daily_high_value,
        })

    def _reset_daily_if_needed(self, account_value: float) -> None:
        """Reset daily high watermark at UTC day boundary."""
        today = datetime.now(timezone.utc).date()
        if self.daily_high_date != today:
            self.daily_high_value = account_value
            self.daily_high_date = today
            self.circuit_breaker_active = False
            self.circuit_breaker_date = None
            self._persist_state()
        elif self.daily_high_value is None or account_value > self.daily_high_value:
            self.daily_high_value = account_value
            self._persist_state()

    # ------------------------------------------------------------------
    # Individual checks — each returns (allowed: bool, reason: str)
    # ------------------------------------------------------------------

    def check_position_size(self, alloc_usd: float, account_value: float) -> tuple[bool, str]:
        """Single position cannot exceed max_position_pct of account."""
        if account_value <= 0:
            return False, "Account value is zero or negative"
        max_alloc = account_value * (self.max_position_pct / 100.0)
        if alloc_usd > max_alloc:
            return False, (
                f"Allocation ${alloc_usd:.2f} exceeds {self.max_position_pct}% "
                f"of account (${max_alloc:.2f})"
            )
        return True, ""

    def check_total_exposure(
        self, positions: list[dict], new_alloc: float, account_value: float
    ) -> tuple[bool, str]:
        """Sum of all position notionals + new allocation cannot exceed max_total_exposure_pct."""
        current_exposure = 0.0
        for pos in positions:
            qty = abs(float(pos.get("quantity") or pos.get("szi") or 0))
            entry = float(pos.get("entry_price") or pos.get("entryPx") or 0)
            current_exposure += qty * entry
        total = current_exposure + new_alloc
        max_exposure = account_value * (self.max_total_exposure_pct / 100.0)
        if total > max_exposure:
            return False, (
                f"Total exposure ${total:.2f} would exceed {self.max_total_exposure_pct}% "
                f"of account (${max_exposure:.2f})"
            )
        return True, ""

    def check_leverage(self, alloc_usd: float, account_value: float) -> tuple[bool, str]:
        """Check that the notional exposure relative to account equity is within limits.

        Hyperliquid leverage is enforced at the position level by the exchange.
        This check prevents the bot from sizing a position so large that the
        implicit leverage (notional / account_value) exceeds our configured max.
        """
        if account_value <= 0:
            return False, "Account value is zero or negative"
        # alloc_usd here is NOTIONAL exposure, account_value is total equity
        implicit_leverage = alloc_usd / account_value
        if implicit_leverage > self.max_leverage:
            return False, (
                f"Implicit leverage {implicit_leverage:.1f}x "
                f"(notional ${alloc_usd:.2f} / equity ${account_value:.2f}) "
                f"exceeds max {self.max_leverage}x"
            )
        return True, ""

    def check_daily_drawdown(self, account_value: float) -> tuple[bool, str]:
        """Activate circuit breaker if account drops max % from daily high."""
        self._reset_daily_if_needed(account_value)
        if self.circuit_breaker_active:
            return False, "Daily loss circuit breaker is active — no new trades until tomorrow (UTC)"
        if self.daily_high_value and self.daily_high_value > 0:
            drawdown_pct = (
                (self.daily_high_value - account_value) / self.daily_high_value * 100
            )
            if drawdown_pct >= self.daily_loss_circuit_breaker_pct:
                self.circuit_breaker_active = True
                self.circuit_breaker_date = datetime.now(timezone.utc).date()
                self._persist_state()
                return False, (
                    f"Daily drawdown {drawdown_pct:.2f}% exceeds circuit breaker "
                    f"threshold of {self.daily_loss_circuit_breaker_pct}%"
                )
        return True, ""

    def check_concurrent_positions(self, current_count: int) -> tuple[bool, str]:
        """Limit number of simultaneous open positions."""
        if current_count >= self.max_concurrent_positions:
            return False, (
                f"Already at max concurrent positions ({self.max_concurrent_positions})"
            )
        return True, ""

    def check_balance_reserve(self, balance: float, account_value: float) -> tuple[bool, str]:
        """Don't trade if balance falls below reserve threshold of CURRENT account value.

        Uses current account_value (not a stale initial snapshot) so the reserve
        scales with account size rather than anchoring to a potentially outdated figure.
        """
        if account_value <= 0:
            return True, ""
        min_balance = account_value * (self.min_balance_reserve_pct / 100.0)
        if balance < min_balance:
            return False, (
                f"Available balance ${balance:.2f} below minimum reserve "
                f"${min_balance:.2f} ({self.min_balance_reserve_pct}% of account value)"
            )
        return True, ""

    def check_trade_confidence(self, confidence: float) -> tuple[bool, str]:
        """Block low-confidence trades before they reach execution."""
        if confidence < self.min_trade_confidence:
            return False, (
                f"Trade confidence {confidence:.2f} is below minimum "
                f"{self.min_trade_confidence:.2f}"
            )
        return True, ""

    def sanitize_requested_leverage(self, leverage: float | None) -> float:
        """Clamp strategy-provided leverage to the configured safety range."""
        if leverage is None:
            return 1.0
        try:
            leverage = float(leverage)
        except (TypeError, ValueError):
            logging.warning("RISK: Non-numeric leverage value; forcing 1x")
            return 1.0
        if leverage < 1.0:
            logging.warning("RISK: leverage %.2f below 1x; forcing 1x", leverage)
            return 1.0
        if leverage > self.max_leverage:
            logging.warning(
                "RISK: leverage %.2f exceeds max %.2fx; capping",
                leverage,
                self.max_leverage,
            )
            return float(self.max_leverage)
        return float(leverage)

    # ------------------------------------------------------------------
    # Stop-loss and take-profit enforcement
    # ------------------------------------------------------------------

    def enforce_stop_loss(
        self, sl_price: float | None, entry_price: float, is_buy: bool
    ) -> float:
        """Ensure every trade has a valid stop-loss on the CORRECT side.

        Validates provided SL. If missing or invalid (wrong side, absurdly close),
        auto-sets at mandatory_sl_pct from entry.
        """
        sl_distance = entry_price * (self.mandatory_sl_pct / 100.0)
        auto_sl = (
            round(entry_price - sl_distance, 8)
            if is_buy
            else round(entry_price + sl_distance, 8)
        )

        if sl_price is None:
            logging.info(
                "RISK: No SL provided; auto-setting at %.6f (%.1f%% from entry)",
                auto_sl, self.mandatory_sl_pct,
            )
            return auto_sl

        try:
            sl_price = float(sl_price)
        except (TypeError, ValueError):
            logging.warning("RISK: Non-numeric SL; using auto SL %.6f", auto_sl)
            return auto_sl

        # SL must be on the losing side
        if is_buy and sl_price >= entry_price:
            logging.warning(
                "RISK: SL %.6f >= entry %.6f for BUY — INVALID; using auto SL %.6f",
                sl_price, entry_price, auto_sl,
            )
            return auto_sl
        if not is_buy and sl_price <= entry_price:
            logging.warning(
                "RISK: SL %.6f <= entry %.6f for SELL — INVALID; using auto SL %.6f",
                sl_price, entry_price, auto_sl,
            )
            return auto_sl

        # SL must not be so tight it fires immediately on spread (< 0.01% from entry)
        min_distance = entry_price * 0.0001
        actual_distance = abs(entry_price - sl_price)
        if actual_distance < min_distance:
            logging.warning(
                "RISK: SL %.6f too close to entry %.6f (%.4f%%); using auto SL",
                sl_price, entry_price, actual_distance / entry_price * 100,
            )
            return auto_sl

        return round(sl_price, 8)

    def enforce_take_profit(
        self, tp_price: float | None, entry_price: float, is_buy: bool
    ) -> float | None:
        """Validate TP is on the profitable side. Returns None if invalid (clears TP)."""
        if tp_price is None:
            return None

        try:
            tp_price = float(tp_price)
        except (TypeError, ValueError):
            logging.warning("RISK: Non-numeric TP; clearing TP")
            return None

        if is_buy and tp_price <= entry_price:
            logging.warning(
                "RISK: TP %.6f <= entry %.6f for BUY — INVALID; clearing TP",
                tp_price, entry_price,
            )
            return None
        if not is_buy and tp_price >= entry_price:
            logging.warning(
                "RISK: TP %.6f >= entry %.6f for SELL — INVALID; clearing TP",
                tp_price, entry_price,
            )
            return None

        return round(tp_price, 8)

    # ------------------------------------------------------------------
    # Force-close losing positions
    # ------------------------------------------------------------------

    def check_losing_positions(self, positions: list[dict]) -> list[dict]:
        """Return positions that should be force-closed due to excessive loss."""
        to_close = []
        for pos in positions:
            # Handle both raw SDK format (szi/entryPx) and normalized format
            coin = pos.get("coin") or pos.get("symbol")
            entry_px = float(pos.get("entryPx") or pos.get("entry_price") or 0)
            size = float(pos.get("szi") or pos.get("quantity") or 0)
            pnl = float(pos.get("pnl") or pos.get("unrealized_pnl") or 0)

            if entry_px == 0 or size == 0:
                continue

            notional = abs(size) * entry_px
            if notional == 0:
                continue

            loss_pct = abs(pnl / notional) * 100 if pnl < 0 else 0.0

            if loss_pct >= self.max_loss_per_position_pct:
                logging.warning(
                    "RISK: Force-closing %s — loss %.2f%% exceeds max %.2f%%",
                    coin, loss_pct, self.max_loss_per_position_pct,
                )
                to_close.append({
                    "coin": coin,
                    "size": abs(size),
                    "is_long": size > 0,
                    "loss_pct": round(loss_pct, 2),
                    "pnl": round(pnl, 2),
                })
        return to_close

    # ------------------------------------------------------------------
    # Composite validation — run all checks before a trade
    # ------------------------------------------------------------------

    def validate_trade(
        self,
        trade: dict,
        account_state: dict,
        _initial_balance_unused: float,  # kept for API compat, no longer used
    ) -> tuple[bool, str, dict]:
        """Run all safety checks on a proposed trade.

        Args:
            trade: Decision dict with keys:
                asset, action, allocation_usd, tp_price, sl_price,
                confidence, leverage, current_price
            account_state: Fresh account state with keys:
                balance, total_value, positions
            _initial_balance_unused: Deprecated parameter, ignored.

        Returns:
            (allowed, reason, adjusted_trade)
        """
        action = trade.get("action", "hold")
        if action == "hold":
            return True, "", trade

        # --- Confidence check ---
        raw_conf = trade.get("confidence")
        confidence: float | None = None
        if raw_conf is not None:
            try:
                confidence = max(0.0, min(1.0, float(raw_conf)))
            except (TypeError, ValueError):
                confidence = None
        if confidence is not None:
            trade = {**trade, "confidence": round(confidence, 4)}
            ok, reason = self.check_trade_confidence(confidence)
            if not ok:
                return False, reason, trade

        # --- Leverage sanitization ---
        raw_lev = trade.get("leverage") or trade.get("requested_leverage")
        trade = {**trade, "leverage": self.sanitize_requested_leverage(raw_lev)}

        # --- Allocation floor ---
        alloc_usd = float(trade.get("allocation_usd", 0))
        if alloc_usd <= 0:
            return False, "Zero or negative allocation", trade
        if alloc_usd < 11.0:
            alloc_usd = 11.0
            trade = {**trade, "allocation_usd": alloc_usd}
            logging.info("RISK: Bumped allocation to $11 (Hyperliquid $10 minimum)")

        account_value = float(account_state.get("total_value", 0))
        balance = float(account_state.get("balance", 0))
        positions = account_state.get("positions", [])
        is_buy = action == "buy"

        # 1. Daily drawdown circuit breaker
        ok, reason = self.check_daily_drawdown(account_value)
        if not ok:
            return False, reason, trade

        # 2. Balance reserve (uses current account_value, not stale initial)
        ok, reason = self.check_balance_reserve(balance, account_value)
        if not ok:
            return False, reason, trade

        # 3. Position size — cap instead of reject
        ok, reason = self.check_position_size(alloc_usd, account_value)
        if not ok:
            max_alloc = max(account_value * (self.max_position_pct / 100.0), 11.0)
            logging.warning(
                "RISK: Capping allocation from $%.2f to $%.2f", alloc_usd, max_alloc
            )
            alloc_usd = max_alloc
            trade = {**trade, "allocation_usd": alloc_usd}

        # 4. Total exposure
        ok, reason = self.check_total_exposure(positions, alloc_usd, account_value)
        if not ok:
            return False, reason, trade

        # 5. Implicit leverage (notional vs equity)
        ok, reason = self.check_leverage(alloc_usd, account_value)
        if not ok:
            return False, reason, trade

        # 6. Concurrent positions
        active_count = sum(
            1 for p in positions
            if abs(float(p.get("szi") or p.get("quantity") or 0)) > 0
        )
        ok, reason = self.check_concurrent_positions(active_count)
        if not ok:
            return False, reason, trade

        # 7. Enforce SL — validate side and auto-set if missing/invalid
        current_price = float(trade.get("current_price", 0))
        entry_price = current_price if current_price > 0 else 1.0
        sl_price = trade.get("sl_price")
        validated_sl = self.enforce_stop_loss(sl_price, entry_price, is_buy)
        trade = {**trade, "sl_price": validated_sl}

        # 8. Enforce TP — validate side; clear if invalid rather than sending wrong order
        tp_price = trade.get("tp_price")
        validated_tp = self.enforce_take_profit(tp_price, entry_price, is_buy)
        trade = {**trade, "tp_price": validated_tp}

        return True, "", trade

    def get_risk_summary(self) -> dict:
        """Return current risk parameters for inclusion in LLM context."""
        return {
            "max_position_pct": self.max_position_pct,
            "max_loss_per_position_pct": self.max_loss_per_position_pct,
            "max_leverage": self.max_leverage,
            "min_trade_confidence": self.min_trade_confidence,
            "max_total_exposure_pct": self.max_total_exposure_pct,
            "daily_loss_circuit_breaker_pct": self.daily_loss_circuit_breaker_pct,
            "mandatory_sl_pct": self.mandatory_sl_pct,
            "max_concurrent_positions": self.max_concurrent_positions,
            "min_balance_reserve_pct": self.min_balance_reserve_pct,
            "circuit_breaker_active": self.circuit_breaker_active,
        }
