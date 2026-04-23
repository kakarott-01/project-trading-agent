"""Broker interfaces for market-data and execution adapters."""

from __future__ import annotations

from typing import Protocol, Any


class MarketDataPort(Protocol):
    async def preload_assets(self, assets: list[str]) -> None: ...
    async def get_user_state(self) -> dict[str, Any]: ...
    async def get_open_orders(self) -> list[dict[str, Any]]: ...
    async def get_recent_fills(self, limit: int = 50) -> list[dict[str, Any]]: ...
    async def query_order_status(
        self, oid: int | None = None, cloid_raw: str | None = None
    ) -> dict[str, Any] | None: ...
    async def get_current_price(self, asset: str) -> float: ...
    async def get_open_interest(self, asset: str) -> float | None: ...
    async def get_funding_rate(self, asset: str) -> float | None: ...
    async def get_candles(
        self, asset: str, interval: str = "5m", count: int = 100
    ) -> list[dict[str, Any]]: ...


class ExecutionPort(Protocol):
    def generate_client_order_id(self) -> str: ...
    def summarize_order_result(self, order_result: Any) -> dict[str, Any]: ...
    async def place_buy_order(
        self,
        asset: str,
        amount: float,
        slippage: float = 0.01,
        cloid_raw: str | None = None,
    ) -> Any: ...
    async def place_sell_order(
        self,
        asset: str,
        amount: float,
        slippage: float = 0.01,
        cloid_raw: str | None = None,
    ) -> Any: ...
    async def place_limit_buy(
        self,
        asset: str,
        amount: float,
        limit_price: float,
        tif: str = "Gtc",
        cloid_raw: str | None = None,
    ) -> Any: ...
    async def place_limit_sell(
        self,
        asset: str,
        amount: float,
        limit_price: float,
        tif: str = "Gtc",
        cloid_raw: str | None = None,
    ) -> Any: ...
    async def place_take_profit(
        self,
        asset: str,
        is_buy: bool,
        amount: float,
        tp_price: float,
        cloid_raw: str | None = None,
    ) -> Any: ...
    async def place_stop_loss(
        self,
        asset: str,
        is_buy: bool,
        amount: float,
        sl_price: float,
        cloid_raw: str | None = None,
    ) -> Any: ...
    async def close_position_market(
        self,
        asset: str,
        amount: float | None = None,
        slippage: float = 0.01,
        cloid_raw: str | None = None,
    ) -> Any: ...
    async def cancel_order(self, asset: str, oid: int | str) -> Any: ...
    async def cancel_all_orders(self, asset: str) -> Any: ...
    async def set_leverage(self, asset: str, leverage: float, is_cross: bool = True) -> Any: ...
    def extract_oids(self, order_result: Any) -> list[str]: ...
