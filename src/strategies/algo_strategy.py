"""Algo strategy adapter around the existing deterministic agent."""

from __future__ import annotations

from src.agent.algo_decision_maker import AlgoTradingAgent
from src.config import Settings
from src.domain.models import DecisionContext, StrategyResult, TradeIntent
from src.strategies.base import Strategy


class AlgoStrategy(Strategy):
    """Runs the configured custom or built-in algo through the common strategy interface."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.source = "algo"
        self.agent = AlgoTradingAgent(settings=settings)

    async def generate(self, context: DecisionContext) -> StrategyResult:
        import asyncio

        outputs = await asyncio.to_thread(
            self.agent.decide_trade,
            context.assets,
            [snapshot.to_dict() for snapshot in context.market_snapshots],
            context.capital_budget_usd,
            context.account_dashboard.to_dict(),
            {
                "cycle": context.invocation.invocation_count,
                "current_time": context.invocation.current_time.isoformat(),
                "interval": context.invocation.interval,
            },
        )
        reasoning_text = outputs.get("reasoning", "") if isinstance(outputs, dict) else ""
        decisions = outputs.get("trade_decisions", []) if isinstance(outputs, dict) else []
        return StrategyResult(
            source=self.source,
            reasoning=reasoning_text,
            intents=[TradeIntent.from_dict({**decision, "source": self.source}) for decision in decisions],
        )
