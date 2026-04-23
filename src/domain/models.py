"""Typed domain models for trading orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class InvocationMetadata:
    minutes_since_start: float
    current_time: datetime
    invocation_count: int
    interval: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "minutes_since_start": round(self.minutes_since_start, 2),
            "current_time": self.current_time.isoformat(),
            "invocation_count": self.invocation_count,
            "interval": self.interval,
        }


@dataclass(slots=True)
class ActiveTradeRecord:
    asset: str
    is_long: bool
    amount: float
    entry_price: float
    confidence: float | None
    leverage: float | None
    tp_oid: str | None
    sl_oid: str | None
    exit_plan: str
    opened_at: str | None
    order_type: str = "market"
    limit_price: float | None = None
    actual_filled: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActiveTradeRecord":
        return cls(
            asset=str(data.get("asset") or ""),
            is_long=bool(data.get("is_long")),
            amount=float(data.get("amount") or 0.0),
            entry_price=float(data.get("entry_price") or 0.0),
            confidence=float(data["confidence"]) if data.get("confidence") is not None else None,
            leverage=float(data["leverage"]) if data.get("leverage") is not None else None,
            tp_oid=data.get("tp_oid"),
            sl_oid=data.get("sl_oid"),
            exit_plan=str(data.get("exit_plan") or ""),
            opened_at=data.get("opened_at"),
            order_type=str(data.get("order_type") or "market"),
            limit_price=(
                float(data["limit_price"]) if data.get("limit_price") is not None else None
            ),
            actual_filled=(
                float(data["actual_filled"]) if data.get("actual_filled") is not None else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "is_long": self.is_long,
            "amount": self.amount,
            "entry_price": self.entry_price,
            "confidence": self.confidence,
            "leverage": self.leverage,
            "tp_oid": self.tp_oid,
            "sl_oid": self.sl_oid,
            "exit_plan": self.exit_plan,
            "opened_at": self.opened_at,
            "order_type": self.order_type,
            "limit_price": self.limit_price,
            "actual_filled": self.actual_filled,
        }


@dataclass(slots=True)
class AccountDashboard:
    balance: float
    account_value: float
    sharpe_ratio: float
    positions: list[dict[str, Any]]
    active_trades: list[ActiveTradeRecord]
    open_orders: list[dict[str, Any]]
    recent_diary: list[dict[str, Any]]
    recent_fills: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "balance": self.balance,
            "account_value": self.account_value,
            "sharpe_ratio": self.sharpe_ratio,
            "positions": self.positions,
            "active_trades": [trade.to_dict() for trade in self.active_trades],
            "open_orders": self.open_orders,
            "recent_diary": self.recent_diary,
            "recent_fills": self.recent_fills,
        }


@dataclass(slots=True)
class MarketSnapshot:
    asset: str
    current_price: float
    intraday: dict[str, Any]
    long_term: dict[str, Any]
    open_interest: float | None
    funding_rate: float | None
    funding_annualized_pct: float | None
    recent_mid_prices: list[float | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "current_price": self.current_price,
            "intraday": self.intraday,
            "long_term": self.long_term,
            "open_interest": self.open_interest,
            "funding_rate": self.funding_rate,
            "funding_annualized_pct": self.funding_annualized_pct,
            "recent_mid_prices": self.recent_mid_prices,
        }


@dataclass(slots=True)
class TradeIntent:
    asset: str
    action: str
    allocation_usd: float = 0.0
    order_type: str = "market"
    limit_price: float | None = None
    tp_price: float | None = None
    sl_price: float | None = None
    exit_plan: str = ""
    rationale: str = ""
    source: str = "none"
    confidence: float | None = None
    leverage: float | None = None
    current_price: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TradeIntent":
        return cls(
            asset=str(data.get("asset") or ""),
            action=str(data.get("action") or "hold").lower(),
            allocation_usd=float(data.get("allocation_usd") or 0.0),
            order_type=str(data.get("order_type") or "market").lower(),
            limit_price=float(data["limit_price"]) if data.get("limit_price") is not None else None,
            tp_price=float(data["tp_price"]) if data.get("tp_price") is not None else None,
            sl_price=float(data["sl_price"]) if data.get("sl_price") is not None else None,
            exit_plan=str(data.get("exit_plan") or ""),
            rationale=str(data.get("rationale") or ""),
            source=str(data.get("source") or "none"),
            confidence=float(data["confidence"]) if data.get("confidence") is not None else None,
            leverage=float(data["leverage"]) if data.get("leverage") is not None else None,
            current_price=(
                float(data["current_price"]) if data.get("current_price") is not None else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "action": self.action,
            "allocation_usd": round(float(self.allocation_usd), 2),
            "order_type": self.order_type,
            "limit_price": self.limit_price,
            "tp_price": self.tp_price,
            "sl_price": self.sl_price,
            "exit_plan": self.exit_plan,
            "rationale": self.rationale,
            "source": self.source,
            "confidence": self.confidence,
            "leverage": self.leverage,
            "current_price": self.current_price,
        }


@dataclass(slots=True)
class DecisionContext:
    assets: list[str]
    market_snapshots: list[MarketSnapshot]
    account_dashboard: AccountDashboard
    risk_limits: dict[str, Any]
    invocation: InvocationMetadata
    capital_budget_usd: float
    provider_label: str

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "invocation": self.invocation.to_dict(),
            "account": self.account_dashboard.to_dict(),
            "risk_limits": self.risk_limits,
            "market_data": [snapshot.to_dict() for snapshot in self.market_snapshots],
            "execution_mode": {
                "source": self.provider_label,
                "enabled": True,
                "capital_budget_usd": round(self.capital_budget_usd, 2),
            },
            "instructions": {
                "assets": self.assets,
                "requirement": "Return strict JSON per schema.",
            },
        }


@dataclass(slots=True)
class StrategyResult:
    source: str
    reasoning: str = ""
    intents: list[TradeIntent] = field(default_factory=list)
