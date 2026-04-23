"""Risk-aware trade execution service."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from src.application.reconciliation_service import ReconciliationService
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
        reconciliation_service: ReconciliationService,
        diary_path: str = "diary.jsonl",
    ):
        self.broker = broker
        self.risk_manager = risk_manager
        self.reconciliation_service = reconciliation_service
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
            open_orders = await self.broker.get_open_orders()
        except Exception as exc:
            logging.error("Failed to refresh state before trading %s: %s", asset, exc)
            return

        position = self._find_position(fresh_state, asset)
        entry_orders = self._entry_orders_for_asset(open_orders, asset)
        desired_is_long = intent.action == "buy"

        if position is not None:
            current_is_long = float(position.get("szi") or 0.0) > 0
            if current_is_long == desired_is_long:
                logging.info("Skipping %s: live position already matches requested direction", asset)
                self._append_diary(
                    {
                        "timestamp": cycle_start.isoformat(),
                        "asset": asset,
                        "action": "skip_existing_position",
                        "source": intent.source,
                        "rationale": intent.rationale,
                    }
                )
                await self.reconciliation_service.reconcile_active_trades(
                    state=fresh_state,
                    active_trades=active_trades,
                    cycle_start=cycle_start,
                    tracked_assets=[asset],
                )
                return

            await self._close_existing_position(
                asset=asset,
                current_position=position,
                intent=intent,
                cycle_start=cycle_start,
                active_trades=active_trades,
                trade_log=trade_log,
            )
            return

        if entry_orders:
            logging.warning("Skipping %s: existing live entry order already pending", asset)
            self._append_diary(
                {
                    "timestamp": cycle_start.isoformat(),
                    "asset": asset,
                    "action": "skip_pending_entry",
                    "source": intent.source,
                    "rationale": "Existing entry order is still pending on exchange.",
                }
            )
            await self.reconciliation_service.reconcile_active_trades(
                state=fresh_state,
                active_trades=active_trades,
                cycle_start=cycle_start,
                tracked_assets=[asset],
            )
            return

        await self._open_new_position(
            intent=intent,
            cycle_start=cycle_start,
            current_price=current_price,
            fresh_state=fresh_state,
            active_trades=active_trades,
            trade_log=trade_log,
        )

    async def _open_new_position(
        self,
        intent: TradeIntent,
        cycle_start: datetime,
        current_price: float,
        fresh_state: dict[str, Any],
        active_trades: list[ActiveTradeRecord],
        trade_log: list[dict],
    ) -> None:
        intent.current_price = current_price
        allowed, reason, adjusted = self.risk_manager.validate_trade(intent.to_dict(), fresh_state, 0)
        adjusted_intent = TradeIntent.from_dict(adjusted)
        if not allowed:
            logging.warning("RISK BLOCKED %s: %s", intent.asset, reason)
            self._append_diary(
                {
                    "timestamp": cycle_start.isoformat(),
                    "asset": intent.asset,
                    "action": "risk_blocked",
                    "source": intent.source,
                    "reason": reason,
                }
            )
            return

        asset = adjusted_intent.asset
        alloc_usd = adjusted_intent.allocation_usd
        leverage = float(adjusted_intent.leverage or 1.0)
        amount = alloc_usd / current_price
        is_buy = adjusted_intent.action == "buy"

        lev_result = await self.broker.set_leverage(asset, leverage, is_cross=True)
        if not self._is_ok_response(lev_result):
            logging.error(
                "Leverage set failed for %s (%.2fx): %s",
                asset,
                leverage,
                lev_result,
            )
            self._append_diary(
                {
                    "timestamp": cycle_start.isoformat(),
                    "asset": asset,
                    "action": "leverage_failed",
                    "source": adjusted_intent.source,
                    "leverage": leverage,
                }
            )
            return

        logging.info("Leverage set for %s: %.2fx", asset, leverage)

        client_order_id = self.broker.generate_client_order_id()
        self._upsert_active_trade(
            active_trades=active_trades,
            trade=ActiveTradeRecord(
                asset=asset,
                is_long=is_buy,
                amount=amount,
                entry_price=current_price,
                confidence=adjusted_intent.confidence,
                leverage=leverage,
                tp_oid=None,
                sl_oid=None,
                exit_plan=adjusted_intent.exit_plan,
                opened_at=cycle_start.isoformat(),
                order_type=adjusted_intent.order_type,
                limit_price=adjusted_intent.limit_price,
                actual_filled=0.0,
                tp_price=adjusted_intent.tp_price,
                sl_price=adjusted_intent.sl_price,
                entry_oid=None,
                client_order_id=client_order_id,
                status="submitting",
                source=adjusted_intent.source,
                last_synced_at=cycle_start.isoformat(),
            ),
        )
        save_active_trades([trade.to_dict() for trade in active_trades])

        try:
            if adjusted_intent.order_type == "limit" and adjusted_intent.limit_price:
                if is_buy:
                    order_result = await self.broker.place_limit_buy(
                        asset,
                        amount,
                        float(adjusted_intent.limit_price),
                        cloid_raw=client_order_id,
                    )
                else:
                    order_result = await self.broker.place_limit_sell(
                        asset,
                        amount,
                        float(adjusted_intent.limit_price),
                        cloid_raw=client_order_id,
                    )
                logging.info(
                    "LIMIT %s %s  amount=%.6f  price=$%.4f",
                    adjusted_intent.action.upper(),
                    asset,
                    amount,
                    float(adjusted_intent.limit_price),
                )
            else:
                if is_buy:
                    order_result = await self.broker.place_buy_order(
                        asset,
                        amount,
                        cloid_raw=client_order_id,
                    )
                else:
                    order_result = await self.broker.place_sell_order(
                        asset,
                        amount,
                        cloid_raw=client_order_id,
                    )
                logging.info(
                    "%s %s  amount=%.6f  at ~$%.4f",
                    adjusted_intent.action.upper(),
                    asset,
                    amount,
                    current_price,
                )
        except Exception as exc:
            logging.error(
                "Order submission for %s became ambiguous; keeping pending record for reconciliation: %s",
                asset,
                exc,
            )
            pending = self._get_active_trade(active_trades, asset)
            if pending is not None:
                pending.status = "pending_confirmation"
                pending.last_synced_at = cycle_start.isoformat()
                save_active_trades([trade.to_dict() for trade in active_trades])
            self._append_diary(
                {
                    "timestamp": cycle_start.isoformat(),
                    "asset": asset,
                    "action": "entry_pending_confirmation",
                    "source": adjusted_intent.source,
                    "client_order_id": client_order_id,
                    "rationale": adjusted_intent.rationale,
                }
            )
            return

        summary = self.broker.summarize_order_result(order_result)
        if not summary["is_success"] and not summary["statuses"]:
            logging.error("Entry order for %s was rejected: %s", asset, summary["error_messages"])

        await asyncio.sleep(2)
        post_state = await self.broker.get_user_state()
        await self.reconciliation_service.reconcile_active_trades(
            state=post_state,
            active_trades=active_trades,
            cycle_start=cycle_start,
            tracked_assets=[asset],
        )
        synced_trade = self._get_active_trade(active_trades, asset)

        if synced_trade is None:
            self._append_diary(
                {
                    "timestamp": cycle_start.isoformat(),
                    "asset": asset,
                    "action": "entry_not_confirmed",
                    "source": adjusted_intent.source,
                    "client_order_id": client_order_id,
                    "errors": summary["error_messages"],
                }
            )
            return

        trade_log.append(
            {
                "type": adjusted_intent.action,
                "price": synced_trade.entry_price or current_price,
                "amount": synced_trade.amount,
                "exit_plan": adjusted_intent.exit_plan,
                "filled": synced_trade.status == "open_position",
            }
        )

        self._append_diary(
            {
                "timestamp": cycle_start.isoformat(),
                "asset": asset,
                "action": adjusted_intent.action,
                "source": adjusted_intent.source,
                "order_type": synced_trade.order_type,
                "limit_price": synced_trade.limit_price,
                "allocation_usd": alloc_usd,
                "amount": synced_trade.amount,
                "actual_filled": synced_trade.actual_filled,
                "entry_price": synced_trade.entry_price,
                "confidence": synced_trade.confidence,
                "leverage": synced_trade.leverage,
                "tp_price": synced_trade.tp_price,
                "tp_oid": synced_trade.tp_oid,
                "sl_price": synced_trade.sl_price,
                "sl_oid": synced_trade.sl_oid,
                "entry_oid": synced_trade.entry_oid,
                "client_order_id": synced_trade.client_order_id,
                "status": synced_trade.status,
                "exit_plan": synced_trade.exit_plan,
                "rationale": adjusted_intent.rationale,
            }
        )

    async def _close_existing_position(
        self,
        asset: str,
        current_position: dict[str, Any],
        intent: TradeIntent,
        cycle_start: datetime,
        active_trades: list[ActiveTradeRecord],
        trade_log: list[dict],
    ) -> None:
        size = abs(float(current_position.get("szi") or 0.0))
        if size <= 0:
            return

        close_cloid = self.broker.generate_client_order_id()
        try:
            await self.broker.cancel_all_orders(asset)
            result = await self.broker.close_position_market(
                asset,
                amount=size,
                cloid_raw=close_cloid,
            )
            summary = self.broker.summarize_order_result(result)
        except Exception as exc:
            logging.error("Close order for %s became ambiguous: %s", asset, exc)
            self._append_diary(
                {
                    "timestamp": cycle_start.isoformat(),
                    "asset": asset,
                    "action": "close_pending_confirmation",
                    "source": intent.source,
                    "client_order_id": close_cloid,
                }
            )
            return

        await asyncio.sleep(2)
        post_state = await self.broker.get_user_state()
        await self.reconciliation_service.reconcile_active_trades(
            state=post_state,
            active_trades=active_trades,
            cycle_start=cycle_start,
            tracked_assets=[asset],
        )
        still_open = self._get_active_trade(active_trades, asset)
        fully_closed = still_open is None or still_open.status != "open_position"

        trade_log.append(
            {
                "type": "close",
                "price": self._safe_float(current_position.get("entryPx")) or 0.0,
                "amount": size,
                "filled": summary["is_success"],
            }
        )

        self._append_diary(
            {
                "timestamp": cycle_start.isoformat(),
                "asset": asset,
                "action": "close_position",
                "source": intent.source,
                "client_order_id": close_cloid,
                "requested_size": size,
                "closed": fully_closed,
                "errors": summary["error_messages"],
                "rationale": "Opposite-direction signal closes the existing position first; no same-cycle flip.",
            }
        )

    @staticmethod
    def _find_position(state: dict[str, Any], asset: str) -> dict[str, Any] | None:
        for position in state.get("positions", []):
            if position.get("coin") != asset:
                continue
            if abs(float(position.get("szi") or 0.0)) <= 0:
                continue
            return position
        return None

    @staticmethod
    def _is_reduce_only_order(order: dict[str, Any]) -> bool:
        if bool(order.get("reduceOnly") or order.get("reduce_only")):
            return True
        order_type = order.get("orderType")
        return isinstance(order_type, dict) and "trigger" in order_type

    def _entry_orders_for_asset(
        self, open_orders: list[dict[str, Any]], asset: str
    ) -> list[dict[str, Any]]:
        return [
            order
            for order in open_orders
            if order.get("coin") == asset and not self._is_reduce_only_order(order)
        ]

    @staticmethod
    def _is_ok_response(result: Any) -> bool:
        return isinstance(result, dict) and result.get("status") == "ok"

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _get_active_trade(
        active_trades: list[ActiveTradeRecord], asset: str
    ) -> ActiveTradeRecord | None:
        return next((trade for trade in active_trades if trade.asset == asset), None)

    @staticmethod
    def _upsert_active_trade(
        active_trades: list[ActiveTradeRecord],
        trade: ActiveTradeRecord,
    ) -> None:
        active_trades[:] = [existing for existing in active_trades if existing.asset != trade.asset]
        active_trades.append(trade)

    def _append_diary(self, entry: dict) -> None:
        with open(self.diary_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
