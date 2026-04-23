"""State reconciliation and force-close handling."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from src.domain.models import ActiveTradeRecord
from src.exchanges.base import ExecutionPort, MarketDataPort
from src.risk_manager import RiskManager
from src.utils.state_persistence import save_active_trades


class ReconciliationService:
    """Keeps local state aligned with exchange truth."""

    def __init__(
        self,
        broker: MarketDataPort | ExecutionPort,
        risk_manager: RiskManager,
        diary_path: str = "diary.jsonl",
    ):
        self.broker = broker
        self.risk_manager = risk_manager
        self.diary_path = diary_path

    async def force_close_losers(
        self,
        state: dict,
        active_trades: list[ActiveTradeRecord],
        cycle_start: datetime,
    ) -> None:
        to_close = self.risk_manager.check_losing_positions(state["positions"])
        for position in to_close:
            coin = position["coin"]
            size = position["size"]
            is_long = position["is_long"]
            logging.warning(
                "RISK FORCE-CLOSE: %s at %.2f%% loss (PnL: $%.2f)",
                coin,
                position["loss_pct"],
                position["pnl"],
            )
            if is_long:
                await self.broker.place_sell_order(coin, size)
            else:
                await self.broker.place_buy_order(coin, size)
            await self.broker.cancel_all_orders(coin)
            active_trades[:] = [trade for trade in active_trades if trade.asset != coin]
            save_active_trades([trade.to_dict() for trade in active_trades])
            self._append_diary(
                {
                    "timestamp": cycle_start.isoformat(),
                    "asset": coin,
                    "action": "risk_force_close",
                    "loss_pct": position["loss_pct"],
                    "pnl": position["pnl"],
                }
            )

    async def reconcile_active_trades(
        self,
        state: dict,
        active_trades: list[ActiveTradeRecord],
        cycle_start: datetime,
    ) -> list[dict]:
        open_orders = await self.broker.get_open_orders()
        assets_with_positions = {
            pos.get("coin")
            for pos in state["positions"]
            if abs(float(pos.get("szi") or 0)) > 0
        }
        assets_with_orders = {order.get("coin") for order in open_orders if order.get("coin")}
        stale = [
            trade
            for trade in active_trades
            if trade.asset not in assets_with_positions and trade.asset not in assets_with_orders
        ]
        for trade in stale:
            logging.info(
                "Reconciling stale active trade: %s (no position, no orders)", trade.asset
            )
            active_trades.remove(trade)
            self._append_diary(
                {
                    "timestamp": cycle_start.isoformat(),
                    "asset": trade.asset,
                    "action": "reconcile_close",
                    "reason": "no_position_no_orders",
                }
            )
        if stale:
            save_active_trades([trade.to_dict() for trade in active_trades])
        return open_orders

    def _append_diary(self, entry: dict) -> None:
        with open(self.diary_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
