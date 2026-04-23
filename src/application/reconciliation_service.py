"""State reconciliation and protection-order maintenance."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

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

    async def bootstrap_active_trades(
        self,
        active_trades: list[ActiveTradeRecord],
        tracked_assets: list[str],
    ) -> list[dict]:
        """Rebuild local state from exchange truth before the first cycle."""
        state = await self.broker.get_user_state()
        return await self.reconcile_active_trades(
            state=state,
            active_trades=active_trades,
            cycle_start=datetime.now(timezone.utc),
            tracked_assets=tracked_assets,
        )

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
            logging.warning(
                "RISK FORCE-CLOSE: %s at %.2f%% loss (PnL: $%.2f)",
                coin,
                position["loss_pct"],
                position["pnl"],
            )
            try:
                await self.broker.cancel_all_orders(coin)
                close_cloid = self.broker.generate_client_order_id()
                await self.broker.close_position_market(
                    coin,
                    amount=size,
                    cloid_raw=close_cloid,
                )
            except Exception as exc:
                logging.error("Force-close failed for %s: %s", coin, exc)
                continue

            self._append_diary(
                {
                    "timestamp": cycle_start.isoformat(),
                    "asset": coin,
                    "action": "risk_force_close",
                    "loss_pct": position["loss_pct"],
                    "pnl": position["pnl"],
                    "client_order_id": close_cloid,
                }
            )

        if to_close:
            save_active_trades([trade.to_dict() for trade in active_trades])

    async def reconcile_active_trades(
        self,
        state: dict,
        active_trades: list[ActiveTradeRecord],
        cycle_start: datetime,
        tracked_assets: list[str] | None = None,
    ) -> list[dict]:
        open_orders = await self.broker.get_open_orders()
        changed = await self._rebuild_active_trades(
            state=state,
            open_orders=open_orders,
            active_trades=active_trades,
            cycle_start=cycle_start,
            tracked_assets=tracked_assets,
        )

        protection_changed = await self._ensure_protection_orders(
            state=state,
            open_orders=open_orders,
            active_trades=active_trades,
            cycle_start=cycle_start,
        )
        if protection_changed:
            open_orders = await self.broker.get_open_orders()
            changed = (
                await self._rebuild_active_trades(
                    state=state,
                    open_orders=open_orders,
                    active_trades=active_trades,
                    cycle_start=cycle_start,
                    tracked_assets=tracked_assets,
                )
                or changed
            )

        if changed or protection_changed:
            save_active_trades([trade.to_dict() for trade in active_trades])
        return open_orders

    async def _rebuild_active_trades(
        self,
        state: dict,
        open_orders: list[dict],
        active_trades: list[ActiveTradeRecord],
        cycle_start: datetime,
        tracked_assets: list[str] | None,
    ) -> bool:
        existing_by_asset = {trade.asset: trade for trade in active_trades if trade.asset}
        assets_to_track: set[str] = set(tracked_assets or [])
        assets_to_track.update(existing_by_asset)
        assets_to_track.update(
            pos.get("coin")
            for pos in state.get("positions", [])
            if pos.get("coin")
        )
        assets_to_track.update(
            order.get("coin")
            for order in open_orders
            if order.get("coin")
        )

        rebuilt: list[ActiveTradeRecord] = []
        removed_assets: set[str] = set()

        for asset in sorted(a for a in assets_to_track if a):
            existing = existing_by_asset.get(asset)
            position = self._find_position(state, asset)
            entry_orders, reduce_orders = self._split_asset_orders(open_orders, asset)

            if not position and reduce_orders and not entry_orders:
                try:
                    await self.broker.cancel_all_orders(asset)
                    logging.warning(
                        "Cancelled orphaned reduce-only orders for %s with no backing position",
                        asset,
                    )
                except Exception as exc:
                    logging.error("Failed to cancel orphaned reduce-only orders for %s: %s", asset, exc)
                open_orders[:] = [order for order in open_orders if order.get("coin") != asset]
                reduce_orders = []

            if not position and not entry_orders and not reduce_orders:
                pending_status = await self._resolve_pending_status(existing)
                if pending_status is not None:
                    rebuilt.append(
                        self._build_record(
                            asset=asset,
                            existing=existing,
                            position=None,
                            entry_orders=[],
                            reduce_orders=[],
                            cycle_start=cycle_start,
                            forced_status=pending_status,
                        )
                    )
                    continue
                if existing is not None:
                    removed_assets.add(asset)
                continue

            rebuilt.append(
                self._build_record(
                    asset=asset,
                    existing=existing,
                    position=position,
                    entry_orders=entry_orders,
                    reduce_orders=reduce_orders,
                    cycle_start=cycle_start,
                )
            )

        if removed_assets:
            for asset in sorted(removed_assets):
                logging.info(
                    "Reconciling stale active trade: %s (no position, no orders)", asset
                )
                self._append_diary(
                    {
                        "timestamp": cycle_start.isoformat(),
                        "asset": asset,
                        "action": "reconcile_close",
                        "reason": "no_position_no_orders",
                    }
                )

        changed = self._records_changed(active_trades, rebuilt)
        if changed:
            active_trades[:] = rebuilt
        return changed or bool(removed_assets)

    async def _resolve_pending_status(
        self, existing: ActiveTradeRecord | None
    ) -> str | None:
        if existing is None or not existing.client_order_id:
            return None

        status = await self.broker.query_order_status(cloid_raw=existing.client_order_id)
        if not status:
            if existing.status == "pending_confirmation":
                return "pending_confirmation"
            return None
        if status["is_open"]:
            return "pending_entry"
        if status["is_filled"]:
            return "pending_confirmation"
        if status["is_canceled"] or status["is_rejected"]:
            return None
        return "pending_confirmation"

    async def _ensure_protection_orders(
        self,
        state: dict,
        open_orders: list[dict],
        active_trades: list[ActiveTradeRecord],
        cycle_start: datetime,
    ) -> bool:
        changed = False
        for trade in active_trades:
            position = self._find_position(state, trade.asset)
            if not position:
                continue

            size = abs(float(position.get("szi") or 0.0))
            if size <= 0:
                continue

            is_long = float(position.get("szi") or 0.0) > 0
            entry_price = self._safe_float(position.get("entryPx")) or trade.entry_price
            trade.is_long = is_long
            trade.amount = size
            trade.actual_filled = size
            trade.entry_price = entry_price
            trade.leverage = self._extract_leverage(position) or trade.leverage
            trade.status = "open_position"
            trade.last_synced_at = cycle_start.isoformat()

            entry_orders, reduce_orders = self._split_asset_orders(open_orders, trade.asset)
            del entry_orders
            tp_oid, sl_oid, tp_price, sl_price = self._extract_trigger_details(reduce_orders)
            if tp_oid != trade.tp_oid:
                trade.tp_oid = tp_oid
                changed = True
            if sl_oid != trade.sl_oid:
                trade.sl_oid = sl_oid
                changed = True
            if tp_price is not None and tp_price != trade.tp_price:
                trade.tp_price = tp_price
                changed = True
            if sl_price is not None and sl_price != trade.sl_price:
                trade.sl_price = sl_price
                changed = True

            if not trade.sl_price and entry_price > 0:
                trade.sl_price = self.risk_manager.enforce_stop_loss(None, entry_price, is_long)
                changed = True

            if not trade.sl_oid and trade.sl_price:
                try:
                    result = await self.broker.place_stop_loss(
                        trade.asset,
                        is_long,
                        size,
                        trade.sl_price,
                        cloid_raw=self.broker.generate_client_order_id(),
                    )
                    summary = self.broker.summarize_order_result(result)
                    if summary["is_success"] and summary["all_oids"]:
                        trade.sl_oid = summary["all_oids"][0]
                        changed = True
                        logging.info("Installed missing SL for %s", trade.asset)
                except Exception as exc:
                    logging.error("Failed to install SL for %s: %s", trade.asset, exc)

            if not trade.tp_oid and trade.tp_price:
                try:
                    result = await self.broker.place_take_profit(
                        trade.asset,
                        is_long,
                        size,
                        trade.tp_price,
                        cloid_raw=self.broker.generate_client_order_id(),
                    )
                    summary = self.broker.summarize_order_result(result)
                    if summary["is_success"] and summary["all_oids"]:
                        trade.tp_oid = summary["all_oids"][0]
                        changed = True
                        logging.info("Installed missing TP for %s", trade.asset)
                except Exception as exc:
                    logging.error("Failed to install TP for %s: %s", trade.asset, exc)
        return changed

    def _build_record(
        self,
        asset: str,
        existing: ActiveTradeRecord | None,
        position: dict[str, Any] | None,
        entry_orders: list[dict[str, Any]],
        reduce_orders: list[dict[str, Any]],
        cycle_start: datetime,
        forced_status: str | None = None,
    ) -> ActiveTradeRecord:
        entry_order = entry_orders[0] if entry_orders else None
        position_size = abs(float(position.get("szi") or 0.0)) if position else 0.0
        is_long = (
            float(position.get("szi") or 0.0) > 0
            if position
            else bool(entry_order.get("isBuy"))
            if entry_order
            else (existing.is_long if existing else True)
        )
        entry_price = (
            self._safe_float(position.get("entryPx")) if position else None
        ) or (
            self._safe_float(entry_order.get("px")) if entry_order else None
        ) or (
            existing.entry_price if existing else 0.0
        )
        amount = position_size or (
            self._safe_float(entry_order.get("sz")) if entry_order else None
        ) or (
            existing.amount if existing else 0.0
        )
        tp_oid, sl_oid, tp_price_live, sl_price_live = self._extract_trigger_details(reduce_orders)
        tp_price = tp_price_live if tp_price_live is not None else (existing.tp_price if existing else None)
        sl_price = sl_price_live if sl_price_live is not None else (existing.sl_price if existing else None)
        if position and entry_price > 0 and sl_price is None:
            sl_price = self.risk_manager.enforce_stop_loss(None, entry_price, is_long)

        status = forced_status or ("open_position" if position else "pending_entry")
        return ActiveTradeRecord(
            asset=asset,
            is_long=is_long,
            amount=float(amount or 0.0),
            entry_price=float(entry_price or 0.0),
            confidence=existing.confidence if existing else None,
            leverage=(
                self._extract_leverage(position)
                if position
                else existing.leverage if existing else None
            ),
            tp_oid=tp_oid or (existing.tp_oid if existing else None),
            sl_oid=sl_oid or (existing.sl_oid if existing else None),
            exit_plan=existing.exit_plan if existing else "",
            opened_at=(existing.opened_at if existing else cycle_start.isoformat()),
            order_type=(
                existing.order_type
                if existing and existing.order_type
                else self._infer_order_type(entry_order)
            ),
            limit_price=(
                self._safe_float(entry_order.get("px")) if entry_order else None
            ) if entry_order and self._infer_order_type(entry_order) == "limit" else (
                existing.limit_price if existing else None
            ),
            actual_filled=position_size if position else (existing.actual_filled if existing else 0.0),
            tp_price=tp_price,
            sl_price=sl_price,
            entry_oid=str(entry_order.get("oid")) if entry_order and entry_order.get("oid") is not None else (existing.entry_oid if existing else None),
            client_order_id=(
                str(entry_order.get("cloid")) if entry_order and entry_order.get("cloid") is not None else (existing.client_order_id if existing else None)
            ),
            status=status,
            source=existing.source if existing else "exchange_sync",
            last_synced_at=cycle_start.isoformat(),
        )

    @staticmethod
    def _records_changed(
        existing: list[ActiveTradeRecord],
        rebuilt: list[ActiveTradeRecord],
    ) -> bool:
        return [trade.to_dict() for trade in existing] != [trade.to_dict() for trade in rebuilt]

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_leverage(position: dict[str, Any] | None) -> float | None:
        if not position:
            return None
        raw = position.get("leverage")
        if isinstance(raw, dict):
            raw = raw.get("value")
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _find_position(state: dict[str, Any], asset: str) -> dict[str, Any] | None:
        for position in state.get("positions", []):
            if position.get("coin") != asset:
                continue
            if abs(float(position.get("szi") or 0.0)) <= 0:
                continue
            return position
        return None

    def _split_asset_orders(
        self, open_orders: list[dict[str, Any]], asset: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        entry_orders: list[dict[str, Any]] = []
        reduce_orders: list[dict[str, Any]] = []
        for order in open_orders:
            if order.get("coin") != asset:
                continue
            if self._is_reduce_only_order(order):
                reduce_orders.append(order)
            else:
                entry_orders.append(order)
        return entry_orders, reduce_orders

    @staticmethod
    def _is_reduce_only_order(order: dict[str, Any]) -> bool:
        if bool(order.get("reduceOnly") or order.get("reduce_only")):
            return True
        order_type = order.get("orderType")
        return isinstance(order_type, dict) and "trigger" in order_type

    @staticmethod
    def _infer_order_type(order: dict[str, Any] | None) -> str:
        if not order:
            return "market"
        order_type = order.get("orderType")
        if isinstance(order_type, dict) and "limit" in order_type:
            tif = (order_type.get("limit") or {}).get("tif")
            return "market" if tif == "Ioc" else "limit"
        return "market"

    @staticmethod
    def _extract_trigger_details(
        reduce_orders: list[dict[str, Any]]
    ) -> tuple[str | None, str | None, float | None, float | None]:
        tp_oid = None
        sl_oid = None
        tp_price = None
        sl_price = None
        for order in reduce_orders:
            order_type = order.get("orderType")
            trigger = order_type.get("trigger") if isinstance(order_type, dict) else None
            tpsl = (trigger or {}).get("tpsl")
            oid = order.get("oid")
            trigger_px = ReconciliationService._safe_float(
                order.get("triggerPx") or (trigger or {}).get("triggerPx")
            )
            if oid is None:
                continue
            if tpsl == "tp" and tp_oid is None:
                tp_oid = str(oid)
                tp_price = trigger_px
            elif tpsl == "sl" and sl_oid is None:
                sl_oid = str(oid)
                sl_price = trigger_px
        return tp_oid, sl_oid, tp_price, sl_price

    def _append_diary(self, entry: dict) -> None:
        with open(self.diary_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
