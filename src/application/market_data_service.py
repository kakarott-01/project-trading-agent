"""Market-data and dashboard assembly for each trading cycle."""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timezone

from src.domain.models import AccountDashboard, ActiveTradeRecord, MarketSnapshot
from src.exchanges.base import MarketDataPort
from src.indicators.local_indicators import compute_all, last_n, latest
from src.utils.prompt_utils import round_or_none, round_series


def calculate_sharpe(returns: list[dict]) -> float:
    if not returns:
        return 0.0
    vals = [entry.get("pnl", 0) if "pnl" in entry else 0 for entry in returns]
    if not vals:
        return 0.0
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    if var <= 0:
        return 0.0
    return mean / (var ** 0.5)


class MarketDataService:
    """Builds normalized market and account views for strategies and execution."""

    def __init__(self, broker: MarketDataPort, diary_path: str = "diary.jsonl"):
        self.broker = broker
        self.diary_path = diary_path
        self.price_history: dict[str, deque] = {}

    async def fetch_account_state(self) -> tuple[dict, float]:
        state = await self.broker.get_user_state()
        account_value = float(
            state.get("total_value")
            or (state["balance"] + sum(position.get("pnl", 0) for position in state["positions"]))
        )
        return state, account_value

    async def build_dashboard(
        self,
        state: dict,
        account_value: float,
        trade_log: list[dict],
        active_trades: list[ActiveTradeRecord],
        open_orders: list[dict],
        fills: list[dict],
    ) -> AccountDashboard:
        positions = []
        for pos in state["positions"]:
            coin = pos.get("coin")
            current_px = await self.broker.get_current_price(coin) if coin else None
            positions.append(
                {
                    "symbol": coin,
                    "quantity": round_or_none(pos.get("szi"), 6),
                    "entry_price": round_or_none(pos.get("entryPx"), 2),
                    "current_price": round_or_none(current_px, 2),
                    "liquidation_price": round_or_none(
                        pos.get("liquidationPx") or pos.get("liqPx"), 2
                    ),
                    "unrealized_pnl": round_or_none(pos.get("pnl"), 4),
                    "leverage": pos.get("leverage"),
                }
            )

        open_orders_struct = [
            {
                "coin": order.get("coin"),
                "oid": order.get("oid"),
                "is_buy": order.get("isBuy"),
                "size": round_or_none(order.get("sz"), 6),
                "price": round_or_none(order.get("px"), 2),
                "trigger_price": round_or_none(order.get("triggerPx"), 2),
                "order_type": order.get("orderType"),
            }
            for order in (open_orders or [])[:50]
        ]

        recent_fills_struct = []
        for entry in fills[-20:]:
            t_raw = entry.get("time") or entry.get("timestamp")
            timestamp = None
            if t_raw is not None:
                try:
                    t_int = int(t_raw)
                    timestamp = datetime.fromtimestamp(
                        t_int / 1000 if t_int > 1e12 else t_int,
                        tz=timezone.utc,
                    ).isoformat()
                except Exception:
                    timestamp = str(t_raw)
            recent_fills_struct.append(
                {
                    "timestamp": timestamp,
                    "coin": entry.get("coin") or entry.get("asset"),
                    "is_buy": entry.get("isBuy"),
                    "size": round_or_none(entry.get("sz") or entry.get("size"), 6),
                    "price": round_or_none(entry.get("px") or entry.get("price"), 2),
                }
            )

        return AccountDashboard(
            balance=round_or_none(state["balance"], 2) or 0.0,
            account_value=round_or_none(account_value, 2) or 0.0,
            sharpe_ratio=round_or_none(calculate_sharpe(trade_log), 3) or 0.0,
            positions=positions,
            active_trades=active_trades,
            open_orders=open_orders_struct,
            recent_diary=self.load_recent_diary(),
            recent_fills=recent_fills_struct,
        )

    def load_recent_diary(self, limit: int = 10) -> list[dict]:
        entries: list[dict] = []
        try:
            with open(self.diary_path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except FileNotFoundError:
            return []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    async def build_market_snapshots(
        self,
        assets: list[str],
        cycle_start: datetime,
    ) -> tuple[list[MarketSnapshot], dict[str, float]]:
        snapshots: list[MarketSnapshot] = []
        asset_prices: dict[str, float] = {}

        for asset in assets:
            try:
                current_price = await self.broker.get_current_price(asset)
                asset_prices[asset] = current_price
                if asset not in self.price_history:
                    self.price_history[asset] = deque(maxlen=60)
                self.price_history[asset].append(
                    {"t": cycle_start.isoformat(), "mid": round_or_none(current_price, 2)}
                )

                oi = await self.broker.get_open_interest(asset)
                funding = await self.broker.get_funding_rate(asset)
                candles_5m = await self.broker.get_candles(asset, "5m", 100)
                candles_4h = await self.broker.get_candles(asset, "4h", 100)

                intra = compute_all(candles_5m)
                long_term = compute_all(candles_4h)
                recent_mids = [
                    entry["mid"] for entry in list(self.price_history.get(asset, []))[-10:]
                ]
                funding_annualized = round(funding * 24 * 365 * 100, 2) if funding else None

                snapshots.append(
                    MarketSnapshot(
                        asset=asset,
                        current_price=round_or_none(current_price, 2) or 0.0,
                        intraday={
                            "ema20": round_or_none(latest(intra.get("ema20", [])), 2),
                            "macd": round_or_none(latest(intra.get("macd", [])), 2),
                            "rsi7": round_or_none(latest(intra.get("rsi7", [])), 2),
                            "rsi14": round_or_none(latest(intra.get("rsi14", [])), 2),
                            "series": {
                                "ema20": round_series(last_n(intra.get("ema20", []), 10), 2),
                                "macd": round_series(last_n(intra.get("macd", []), 10), 2),
                                "rsi7": round_series(last_n(intra.get("rsi7", []), 10), 2),
                                "rsi14": round_series(last_n(intra.get("rsi14", []), 10), 2),
                            },
                        },
                        long_term={
                            "ema20": round_or_none(latest(long_term.get("ema20", [])), 2),
                            "ema50": round_or_none(latest(long_term.get("ema50", [])), 2),
                            "atr3": round_or_none(latest(long_term.get("atr3", [])), 2),
                            "atr14": round_or_none(latest(long_term.get("atr14", [])), 2),
                            "macd_series": round_series(last_n(long_term.get("macd", []), 10), 2),
                            "rsi_series": round_series(last_n(long_term.get("rsi14", []), 10), 2),
                        },
                        open_interest=round_or_none(oi, 2),
                        funding_rate=round_or_none(funding, 8),
                        funding_annualized_pct=funding_annualized,
                        recent_mid_prices=recent_mids,
                    )
                )
            except Exception as exc:
                logging.error("Data gather error %s: %s", asset, exc)

        return snapshots, asset_prices
