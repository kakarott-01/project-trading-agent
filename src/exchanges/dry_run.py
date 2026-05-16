"""Paper-trading broker wrapper.

The wrapper delegates market-data reads to a real Hyperliquid broker while
simulating every execution write locally.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any

from src.config import Settings
from src.exchanges.base import ExecutionPort, MarketDataPort
from src.utils.log_files import append_jsonl
from src.utils.paths import data_path


class DryRunBroker:
    """Market-data passthrough plus local virtual execution state."""

    dry_run = True

    def __init__(
        self,
        delegate: MarketDataPort | ExecutionPort,
        settings: Settings,
        state_path: str = "dry_run_state.json",
        diary_path: str = "diary.jsonl",
    ):
        self.delegate = delegate
        self.settings = settings
        self.state_path = state_path
        self.diary_path = diary_path
        self.state = self._load_state()
        self._save_state()

    async def preload_assets(self, assets: list[str]) -> None:
        await self.delegate.preload_assets(assets)

    async def validate_assets(self, assets: list[str]) -> None:
        await self.delegate.validate_assets(assets)

    async def get_current_price(self, asset: str) -> float:
        return await self.delegate.get_current_price(asset)

    async def get_open_interest(self, asset: str) -> float | None:
        return await self.delegate.get_open_interest(asset)

    async def get_funding_rate(self, asset: str) -> float | None:
        return await self.delegate.get_funding_rate(asset)

    async def get_candles(
        self, asset: str, interval: str = "5m", count: int = 100
    ) -> list[dict[str, Any]]:
        return await self.delegate.get_candles(asset, interval=interval, count=count)

    def generate_client_order_id(self) -> str:
        return f"0x{secrets.token_hex(16)}"

    def round_size(self, asset: str, amount: float) -> float:
        round_size = getattr(self.delegate, "round_size", None)
        if callable(round_size):
            return float(round_size(asset, amount))
        return round(float(amount), 8)

    def summarize_order_result(self, order_result: Any) -> dict[str, Any]:
        summarize = getattr(self.delegate, "summarize_order_result", None)
        if callable(summarize):
            return summarize(order_result)
        return {
            "ok": isinstance(order_result, dict) and order_result.get("status") == "ok",
            "is_success": isinstance(order_result, dict) and order_result.get("status") == "ok",
            "has_error": False,
            "error_messages": [],
            "statuses": [],
            "resting_oids": [],
            "filled_oids": [],
            "all_oids": [],
            "filled_size": 0.0,
            "avg_fill_price": None,
            "raw": order_result,
        }

    def extract_oids(self, order_result: Any) -> list[str]:
        return self.summarize_order_result(order_result)["all_oids"]

    async def get_open_orders(self) -> list[dict[str, Any]]:
        await self._refresh_trigger_orders()
        return [dict(order) for order in self.state["open_orders"]]

    async def get_recent_fills(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.state.get("fills", []))[-limit:]

    async def query_order_status(
        self, oid: int | None = None, cloid_raw: str | None = None
    ) -> dict[str, Any] | None:
        await self._refresh_trigger_orders()
        for order in self.state["open_orders"]:
            if oid is not None and str(order.get("oid")) == str(oid):
                return self._order_status(order, "open")
            if cloid_raw and str(order.get("cloid")) == str(cloid_raw):
                return self._order_status(order, "open")
        for fill in reversed(self.state.get("fills", [])):
            if oid is not None and str(fill.get("oid")) == str(oid):
                return self._fill_status(fill)
            if cloid_raw and str(fill.get("cloid")) == str(cloid_raw):
                return self._fill_status(fill)
        return None

    async def get_user_state(
        self, open_orders: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        del open_orders
        await self._refresh_trigger_orders()
        positions = []
        unrealized = 0.0
        margin_used = 0.0
        for asset, position in list(self.state["positions"].items()):
            current_px = await self.get_current_price(asset)
            size_signed = float(position.get("szi") or 0.0)
            if abs(size_signed) <= 0:
                continue
            entry_px = float(position.get("entryPx") or current_px or 0.0)
            pnl = self._pnl(size_signed, entry_px, current_px)
            unrealized += pnl
            margin = self._position_margin(position)
            margin_used += margin
            normalized = {
                "coin": asset,
                "szi": size_signed,
                "entryPx": entry_px,
                "markPx": current_px,
                "pnl": pnl,
                "notional_entry": abs(size_signed) * entry_px,
                "notional_current": abs(size_signed) * current_px,
                "position_value": abs(size_signed) * current_px,
                "leverage": {"type": "cross", "value": position.get("leverage", 1.0)},
                "margin_used": margin,
                "dry_run": True,
            }
            positions.append(normalized)

        cash = float(self.state["cash"])
        return {
            "balance": cash,
            "total_value": cash + margin_used + unrealized,
            "positions": positions,
            "pending_entry_orders": [],
            "pending_entry_assets": [],
            "dry_run": True,
        }

    async def place_buy_order(
        self,
        asset: str,
        amount: float,
        slippage: float = 0.01,
        cloid_raw: str | None = None,
    ) -> Any:
        del slippage
        return await self._open_market(asset, True, amount, cloid_raw)

    async def place_sell_order(
        self,
        asset: str,
        amount: float,
        slippage: float = 0.01,
        cloid_raw: str | None = None,
    ) -> Any:
        del slippage
        return await self._open_market(asset, False, amount, cloid_raw)

    async def place_limit_buy(
        self,
        asset: str,
        amount: float,
        limit_price: float,
        tif: str = "Gtc",
        cloid_raw: str | None = None,
    ) -> Any:
        del limit_price, tif, cloid_raw
        raise RuntimeError("Dry-run limit entries are disabled; execution uses market orders")

    async def place_limit_sell(
        self,
        asset: str,
        amount: float,
        limit_price: float,
        tif: str = "Gtc",
        cloid_raw: str | None = None,
    ) -> Any:
        del limit_price, tif, cloid_raw
        raise RuntimeError("Dry-run limit entries are disabled; execution uses market orders")

    async def place_take_profit(
        self,
        asset: str,
        is_buy: bool,
        amount: float,
        tp_price: float,
        cloid_raw: str | None = None,
    ) -> Any:
        return await self._place_trigger(asset, is_buy, amount, tp_price, "tp", cloid_raw)

    async def place_stop_loss(
        self,
        asset: str,
        is_buy: bool,
        amount: float,
        sl_price: float,
        cloid_raw: str | None = None,
    ) -> Any:
        return await self._place_trigger(asset, is_buy, amount, sl_price, "sl", cloid_raw)

    async def close_position_market(
        self,
        asset: str,
        amount: float | None = None,
        slippage: float = 0.01,
        cloid_raw: str | None = None,
    ) -> Any:
        del slippage
        price = await self.get_current_price(asset)
        oid = self._next_oid()
        closed_size = self._close_position(
            asset,
            price,
            amount=amount,
            oid=oid,
            cloid=cloid_raw,
            reason="market_close",
        )
        self._save_state()
        return self._filled_result(oid, closed_size, price)

    async def cancel_order(self, asset: str, oid: int | str) -> Any:
        before = len(self.state["open_orders"])
        self.state["open_orders"] = [
            order
            for order in self.state["open_orders"]
            if not (order.get("coin") == asset and str(order.get("oid")) == str(oid))
        ]
        self._save_state()
        return {"status": "ok", "cancelled_count": before - len(self.state["open_orders"])}

    async def cancel_all_orders(self, asset: str) -> Any:
        before = len(self.state["open_orders"])
        self.state["open_orders"] = [
            order for order in self.state["open_orders"] if order.get("coin") != asset
        ]
        cancelled = before - len(self.state["open_orders"])
        self._save_state()
        return {"status": "ok", "cancelled_count": cancelled, "remaining_count": 0}

    async def set_leverage(
        self, asset: str, leverage: float, is_cross: bool = True
    ) -> Any:
        del is_cross
        lev = max(1.0, float(leverage or 1.0))
        self.state["leverage"][asset] = lev
        if asset in self.state["positions"]:
            self.state["positions"][asset]["leverage"] = lev
        self._save_state()
        return {"status": "ok", "response": {"data": {"statuses": [{"success": True}]}}}

    async def _open_market(
        self,
        asset: str,
        is_buy: bool,
        amount: float,
        cloid_raw: str | None,
    ) -> Any:
        price = await self.get_current_price(asset)
        amount = self.round_size(asset, amount)
        if amount <= 0 or price <= 0:
            return {"status": "error", "error": "Dry-run order amount or price is invalid"}

        existing = self.state["positions"].get(asset)
        if existing and float(existing.get("szi") or 0.0) * (1 if is_buy else -1) < 0:
            self._close_position(
                asset,
                price,
                amount=None,
                oid=self._next_oid(),
                cloid=cloid_raw,
                reason="dry_run_flip_flatten",
            )

        leverage = max(float(self.state["leverage"].get(asset, 1.0) or 1.0), 1.0)
        margin_required = amount * price / leverage
        if margin_required > float(self.state["cash"]):
            return {
                "status": "error",
                "error": (
                    f"Insufficient dry-run cash for margin: requires ${margin_required:.2f}, "
                    f"available ${float(self.state['cash']):.2f}"
                ),
            }

        oid = self._next_oid()
        signed_size = amount if is_buy else -amount
        self.state["cash"] = float(self.state["cash"]) - margin_required
        self.state["positions"][asset] = {
            "coin": asset,
            "szi": signed_size,
            "entryPx": price,
            "leverage": leverage,
            "margin": margin_required,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        self._record_fill(
            asset=asset,
            oid=oid,
            cloid=cloid_raw,
            side="buy" if is_buy else "sell",
            size=amount,
            price=price,
            realized_pnl=0.0,
            reason="dry_run_entry",
        )
        self._save_state()
        return self._filled_result(oid, amount, price)

    async def _place_trigger(
        self,
        asset: str,
        is_buy: bool,
        amount: float,
        trigger_price: float,
        tpsl: str,
        cloid_raw: str | None,
    ) -> Any:
        amount = self.round_size(asset, amount)
        oid = self._next_oid()
        order = {
            "coin": asset,
            "oid": oid,
            "cloid": cloid_raw,
            "isBuy": not is_buy,
            "sz": amount,
            "px": trigger_price,
            "reduceOnly": True,
            "orderType": {
                "trigger": {
                    "triggerPx": trigger_price,
                    "isMarket": True,
                    "tpsl": tpsl,
                }
            },
            "triggerPx": trigger_price,
            "dry_run": True,
        }
        self.state["open_orders"] = [
            existing
            for existing in self.state["open_orders"]
            if not (
                existing.get("coin") == asset
                and (existing.get("orderType", {}).get("trigger") or {}).get("tpsl") == tpsl
            )
        ]
        self.state["open_orders"].append(order)
        self._save_state()
        return self._resting_result(oid)

    async def _refresh_trigger_orders(self) -> None:
        remaining = []
        changed = False
        for order in list(self.state["open_orders"]):
            asset = str(order.get("coin") or "")
            position = self.state["positions"].get(asset)
            if not position:
                changed = True
                continue
            current_price = await self.get_current_price(asset)
            trigger = (order.get("orderType", {}).get("trigger") or {})
            trigger_px = float(trigger.get("triggerPx") or order.get("triggerPx") or 0.0)
            tpsl = trigger.get("tpsl")
            size_signed = float(position.get("szi") or 0.0)
            is_long = size_signed > 0
            hit = (
                (tpsl == "tp" and ((is_long and current_price >= trigger_px) or (not is_long and current_price <= trigger_px)))
                or (tpsl == "sl" and ((is_long and current_price <= trigger_px) or (not is_long and current_price >= trigger_px)))
            )
            if hit:
                self._close_position(
                    asset,
                    current_price,
                    amount=float(order.get("sz") or 0.0),
                    oid=int(order["oid"]),
                    cloid=order.get("cloid"),
                    reason=f"dry_run_{tpsl}_trigger",
                )
                changed = True
            else:
                remaining.append(order)
        if changed:
            self.state["open_orders"] = remaining
            self._save_state()

    def _close_position(
        self,
        asset: str,
        price: float,
        *,
        amount: float | None,
        oid: int,
        cloid: str | None,
        reason: str,
    ) -> float:
        position = self.state["positions"].get(asset)
        if not position:
            return 0.0
        size_signed = float(position.get("szi") or 0.0)
        entry_px = float(position.get("entryPx") or price)
        close_size = min(abs(size_signed), abs(float(amount or size_signed)))
        if close_size <= 0:
            return 0.0
        realized_pnl = self._pnl(size_signed, entry_px, price, size=close_size)
        margin = self._position_margin(position)
        margin_released = margin * (close_size / abs(size_signed))
        self.state["cash"] = float(self.state["cash"]) + margin_released + realized_pnl
        remaining_size = abs(size_signed) - close_size
        if remaining_size <= 1e-12:
            self.state["positions"].pop(asset, None)
            self.state["open_orders"] = [
                order for order in self.state["open_orders"] if order.get("coin") != asset
            ]
        else:
            self.state["positions"][asset]["szi"] = remaining_size if size_signed > 0 else -remaining_size
            self.state["positions"][asset]["margin"] = margin - margin_released
        self._record_fill(
            asset=asset,
            oid=oid,
            cloid=cloid,
            side="sell" if size_signed > 0 else "buy",
            size=close_size,
            price=price,
            realized_pnl=realized_pnl,
            margin_released=margin_released,
            reason=reason,
        )
        return close_size

    def _record_fill(
        self,
        *,
        asset: str,
        oid: int,
        cloid: str | None,
        side: str,
        size: float,
        price: float,
        realized_pnl: float,
        reason: str,
        margin_released: float = 0.0,
    ) -> None:
        fill = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "coin": asset,
            "oid": oid,
            "cloid": cloid,
            "side": side,
            "sz": size,
            "px": price,
            "realized_pnl": realized_pnl,
            "margin_released": margin_released,
            "reason": reason,
            "dry_run": True,
        }
        self.state.setdefault("fills", []).append(fill)
        self.state["fills"] = self.state["fills"][-500:]
        append_jsonl(
            self.diary_path,
            {
                "timestamp": fill["timestamp"],
                "asset": asset,
                "action": reason,
                "side": side,
                "amount": size,
                "price": price,
                "realized_pnl": round(realized_pnl, 8),
                "margin_released": round(margin_released, 8),
                "client_order_id": cloid,
                "dry_run": True,
            },
        )

    def _load_state(self) -> dict[str, Any]:
        path = data_path(self.state_path)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    state = json.load(handle)
                if isinstance(state, dict):
                    state.setdefault("cash", self.settings.runtime.dry_run_initial_balance)
                    state.setdefault("positions", {})
                    state.setdefault("open_orders", [])
                    state.setdefault("leverage", {})
                    state.setdefault("fills", [])
                    state.setdefault("next_oid", 1)
                    self._migrate_position_margins(state)
                    return state
            except (OSError, json.JSONDecodeError) as exc:
                logging.error("Failed to load dry-run state; starting fresh: %s", exc)
        return {
            "cash": self.settings.runtime.dry_run_initial_balance,
            "positions": {},
            "open_orders": [],
            "leverage": {},
            "fills": [],
            "next_oid": 1,
        }

    @staticmethod
    def _migrate_position_margins(state: dict[str, Any]) -> None:
        migrated_margin = 0.0
        for position in state.get("positions", {}).values():
            if "margin" in position:
                continue
            leverage = max(float(position.get("leverage") or 1.0), 1.0)
            size = abs(float(position.get("szi") or 0.0))
            entry_px = float(position.get("entryPx") or 0.0)
            margin = size * entry_px / leverage
            position["margin"] = margin
            migrated_margin += margin
        if migrated_margin > 0:
            state["cash"] = float(state.get("cash", 0.0)) - migrated_margin

    @staticmethod
    def _position_margin(position: dict[str, Any]) -> float:
        if position.get("margin") is not None:
            return max(float(position.get("margin") or 0.0), 0.0)
        leverage = max(float(position.get("leverage") or 1.0), 1.0)
        size = abs(float(position.get("szi") or 0.0))
        entry_px = float(position.get("entryPx") or 0.0)
        return size * entry_px / leverage

    def _save_state(self) -> None:
        path = data_path(self.state_path)
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.state, handle, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def _next_oid(self) -> int:
        oid = int(self.state.get("next_oid", 1))
        self.state["next_oid"] = oid + 1
        return oid

    @staticmethod
    def _pnl(
        size_signed: float,
        entry_px: float,
        current_px: float,
        *,
        size: float | None = None,
    ) -> float:
        direction = 1.0 if size_signed > 0 else -1.0
        size_abs = abs(size_signed) if size is None else abs(size)
        return (current_px - entry_px) * size_abs * direction

    @staticmethod
    def _filled_result(oid: int, size: float, price: float) -> dict[str, Any]:
        return {
            "status": "ok",
            "response": {
                "data": {
                    "statuses": [
                        {
                            "filled": {
                                "oid": oid,
                                "totalSz": str(size),
                                "avgPx": str(price),
                            }
                        }
                    ]
                }
            },
        }

    @staticmethod
    def _resting_result(oid: int) -> dict[str, Any]:
        return {
            "status": "ok",
            "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}},
        }

    @staticmethod
    def _order_status(order: dict[str, Any], status: str) -> dict[str, Any]:
        return {
            "exists": True,
            "status": status,
            "is_open": status == "open",
            "is_filled": False,
            "is_canceled": False,
            "is_rejected": False,
            "is_final": False,
            "oid": order.get("oid"),
            "cloid": order.get("cloid"),
            "coin": order.get("coin"),
            "size": order.get("sz"),
            "raw": order,
        }

    @staticmethod
    def _fill_status(fill: dict[str, Any]) -> dict[str, Any]:
        return {
            "exists": True,
            "status": "filled",
            "is_open": False,
            "is_filled": True,
            "is_canceled": False,
            "is_rejected": False,
            "is_final": True,
            "oid": fill.get("oid"),
            "cloid": fill.get("cloid"),
            "coin": fill.get("coin"),
            "size": fill.get("sz"),
            "raw": fill,
        }
