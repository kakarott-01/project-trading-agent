"""Risk-aware trade execution service."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from src.domain.models import ActiveTradeRecord, TradeIntent
from src.exchanges.base import ExecutionPort, MarketDataPort
from src.risk_manager import RiskManager
from src.utils.state_persistence import save_active_trades


class ExecutionService:
    """Executes normalized trade intents after risk validation."""

    def __init__(
        self,
        broker: MarketDataPort | ExecutionPort,
        risk_manager: RiskManager,
        diary_path: str = "diary.jsonl",
    ):
        self.broker = broker
        self.risk_manager = risk_manager
        self.diary_path = diary_path

    async def execute(
        self,
        intents: list[TradeIntent],
        assets: list[str],
        cycle_start: datetime,
        asset_prices: dict[str, float],
        active_trades: list[ActiveTradeRecord],
        shutdown_event: asyncio.Event,
        trade_log: list[dict],
    ) -> None:
        for intent in intents:
            if shutdown_event.is_set():
                logging.info("Shutdown requested — skipping remaining trades this cycle")
                break
            await self._execute_intent(
                intent=intent,
                assets=assets,
                cycle_start=cycle_start,
                asset_prices=asset_prices,
                active_trades=active_trades,
                trade_log=trade_log,
            )

    async def _execute_intent(
        self,
        intent: TradeIntent,
        assets: list[str],
        cycle_start: datetime,
        asset_prices: dict[str, float],
        active_trades: list[ActiveTradeRecord],
        trade_log: list[dict],
    ) -> None:
        asset = intent.asset
        if not asset or asset not in assets:
            return

        current_price = asset_prices.get(asset, 0.0)
        if current_price <= 0:
            logging.warning("Skipping %s: invalid current price (%s)", asset, current_price)
            return

        if intent.rationale:
            logging.info("Decision [%s] %s: %s", intent.source, asset, intent.rationale)

        if intent.action not in ("buy", "sell"):
            logging.info("Hold %s: %s", asset, intent.rationale)
            self._append_diary(
                {
                    "timestamp": cycle_start.isoformat(),
                    "asset": asset,
                    "action": "hold",
                    "source": intent.source,
                    "rationale": intent.rationale,
                }
            )
            return

        if intent.allocation_usd <= 0:
            logging.info("Skipping %s: zero allocation", asset)
            return

        try:
            fresh_state = await self.broker.get_user_state()
        except Exception as exc:
            logging.error("Failed to refresh state before trading %s: %s", asset, exc)
            return

        intent.current_price = current_price
        allowed, reason, adjusted = self.risk_manager.validate_trade(intent.to_dict(), fresh_state, 0)
        adjusted_intent = TradeIntent.from_dict(adjusted)
        if not allowed:
            logging.warning("RISK BLOCKED %s: %s", asset, reason)
            self._append_diary(
                {
                    "timestamp": cycle_start.isoformat(),
                    "asset": asset,
                    "action": "risk_blocked",
                    "source": intent.source,
                    "reason": reason,
                }
            )
            return

        alloc_usd = adjusted_intent.allocation_usd
        confidence = adjusted_intent.confidence
        leverage = float(adjusted_intent.leverage or 1.0)
        amount = alloc_usd / current_price
        is_buy = adjusted_intent.action == "buy"

        lev_result = await self.broker.set_leverage(asset, leverage, is_cross=True)
        if isinstance(lev_result, dict) and lev_result.get("status") == "error":
            logging.error(
                "Leverage set failed for %s (%.2fx): %s",
                asset,
                leverage,
                lev_result.get("message"),
            )
        else:
            logging.info("Leverage set for %s: %.2fx", asset, leverage)

        order_type = adjusted_intent.order_type
        limit_price = adjusted_intent.limit_price
        if order_type == "limit" and limit_price:
            if is_buy:
                await self.broker.place_limit_buy(asset, amount, float(limit_price))
            else:
                await self.broker.place_limit_sell(asset, amount, float(limit_price))
            logging.info(
                "LIMIT %s %s  amount=%.6f  price=$%.4f",
                adjusted_intent.action.upper(),
                asset,
                amount,
                float(limit_price),
            )
        else:
            order_type = "market"
            limit_price = None
            if is_buy:
                await self.broker.place_buy_order(asset, amount)
            else:
                await self.broker.place_sell_order(asset, amount)
            logging.info(
                "%s %s  amount=%.6f  at ~$%.4f",
                adjusted_intent.action.upper(),
                asset,
                amount,
                current_price,
            )

        await asyncio.sleep(2)
        fills_check = await self.broker.get_recent_fills(limit=20)
        actual_filled = 0.0
        for fill in reversed(fills_check):
            if fill.get("coin") == asset or fill.get("asset") == asset:
                try:
                    actual_filled += float(fill.get("sz") or fill.get("size") or 0)
                except Exception:
                    pass
                break

        is_limit_resting = order_type == "limit" and actual_filled == 0.0
        tp_oid = None
        sl_oid = None
        if actual_filled > 0:
            if adjusted_intent.tp_price:
                try:
                    tp_order = await self.broker.place_take_profit(
                        asset, is_buy, actual_filled, adjusted_intent.tp_price
                    )
                    tp_oids = self.broker.extract_oids(tp_order)
                    tp_oid = tp_oids[0] if tp_oids else None
                    logging.info("TP placed %s at %s", asset, adjusted_intent.tp_price)
                except Exception as exc:
                    logging.error("TP placement failed for %s: %s", asset, exc)

            if adjusted_intent.sl_price:
                try:
                    sl_order = await self.broker.place_stop_loss(
                        asset, is_buy, actual_filled, adjusted_intent.sl_price
                    )
                    sl_oids = self.broker.extract_oids(sl_order)
                    sl_oid = sl_oids[0] if sl_oids else None
                    logging.info("SL placed %s at %s", asset, adjusted_intent.sl_price)
                except Exception as exc:
                    logging.error("SL placement failed for %s: %s", asset, exc)
        elif not is_limit_resting:
            logging.warning(
                "No fill confirmed for %s after order placement — TP/SL NOT placed to avoid orphan orders",
                asset,
            )

        trade_log.append(
            {
                "type": adjusted_intent.action,
                "price": current_price,
                "amount": amount,
                "exit_plan": adjusted_intent.exit_plan,
                "filled": actual_filled > 0,
            }
        )

        active_trades[:] = [trade for trade in active_trades if trade.asset != asset]
        active_trades.append(
            ActiveTradeRecord(
                asset=asset,
                is_long=is_buy,
                amount=amount,
                entry_price=current_price,
                confidence=confidence,
                leverage=leverage,
                tp_oid=tp_oid,
                sl_oid=sl_oid,
                exit_plan=adjusted_intent.exit_plan,
                opened_at=cycle_start.isoformat(),
                order_type=order_type,
                limit_price=limit_price,
                actual_filled=actual_filled,
            )
        )
        save_active_trades([trade.to_dict() for trade in active_trades])

        self._append_diary(
            {
                "timestamp": cycle_start.isoformat(),
                "asset": asset,
                "action": adjusted_intent.action,
                "source": adjusted_intent.source,
                "order_type": order_type,
                "limit_price": limit_price,
                "allocation_usd": alloc_usd,
                "amount": amount,
                "actual_filled": actual_filled,
                "entry_price": current_price,
                "confidence": confidence,
                "leverage": leverage,
                "tp_price": adjusted_intent.tp_price,
                "tp_oid": tp_oid,
                "sl_price": adjusted_intent.sl_price,
                "sl_oid": sl_oid,
                "exit_plan": adjusted_intent.exit_plan,
                "rationale": adjusted_intent.rationale,
            }
        )

    def _append_diary(self, entry: dict) -> None:
        with open(self.diary_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
