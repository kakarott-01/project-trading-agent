"""Strategy execution, allocation scaling, and signal merge logic."""

from __future__ import annotations

import json
import logging
from collections import OrderedDict

from src.domain.models import DecisionContext, StrategyResult, TradeIntent
from src.strategies.base import Strategy
from src.utils.prompt_utils import json_default


def _to_float_or_zero(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class DecisionPipeline:
    """Runs strategies through a normalized merge pipeline."""

    def __init__(self, prompt_log_path: str = "prompts.log"):
        self.prompt_log_path = prompt_log_path

    @staticmethod
    def scale_decision_allocations(
        decisions: list[TradeIntent], capital_budget_usd: float
    ) -> list[TradeIntent]:
        normalized: list[TradeIntent] = []
        actionable_total = 0.0

        for decision in decisions:
            item = TradeIntent.from_dict(decision.to_dict())
            item.allocation_usd = max(0.0, _to_float_or_zero(item.allocation_usd))
            normalized.append(item)
            if item.action in ("buy", "sell"):
                actionable_total += item.allocation_usd

        if actionable_total <= 0:
            return normalized

        if capital_budget_usd <= 0:
            for item in normalized:
                if item.action in ("buy", "sell"):
                    item.action = "hold"
                    item.allocation_usd = 0.0
                    item.rationale = (
                        f"{item.rationale} Capital budget is 0 for this mode."
                    ).strip()
            return normalized

        scale = min(1.0, capital_budget_usd / actionable_total)
        for item in normalized:
            if item.action in ("buy", "sell"):
                item.allocation_usd = round(item.allocation_usd * scale, 2)
        return normalized

    @staticmethod
    def merge_trade_decisions(
        all_decisions: list[TradeIntent], assets: list[str]
    ) -> list[TradeIntent]:
        grouped: dict[str, list[TradeIntent]] = {asset: [] for asset in assets}
        for decision in all_decisions:
            if decision.asset in grouped:
                grouped[decision.asset].append(decision)

        merged: list[TradeIntent] = []
        for asset in assets:
            source_decisions = grouped.get(asset, [])
            actionable = [
                decision
                for decision in source_decisions
                if decision.action in ("buy", "sell") and decision.allocation_usd > 0
            ]
            sources = sorted({decision.source or "unknown" for decision in source_decisions})

            if not actionable:
                rationale_parts = [decision.rationale for decision in source_decisions if decision.rationale]
                merged.append(
                    TradeIntent(
                        asset=asset,
                        action="hold",
                        allocation_usd=0.0,
                        rationale=" | ".join(rationale_parts)
                        if rationale_parts
                        else "No actionable signal.",
                        source="+".join(sources) if sources else "none",
                        confidence=0.0,
                        leverage=1.0,
                    )
                )
                continue

            action_set = {decision.action for decision in actionable}
            if len(action_set) > 1:
                merged.append(
                    TradeIntent(
                        asset=asset,
                        action="hold",
                        allocation_usd=0.0,
                        rationale="Conflict between enabled strategies; holding this cycle.",
                        source="+".join(sorted({decision.source or "unknown" for decision in actionable})),
                        confidence=0.0,
                        leverage=1.0,
                    )
                )
                continue

            preferred = max(actionable, key=lambda decision: decision.allocation_usd)
            total_alloc = sum(decision.allocation_usd for decision in actionable)
            merged_decision = TradeIntent(
                asset=asset,
                action=preferred.action,
                allocation_usd=round(total_alloc, 2),
                order_type=preferred.order_type,
                limit_price=preferred.limit_price,
                tp_price=preferred.tp_price,
                sl_price=preferred.sl_price,
                exit_plan=preferred.exit_plan,
                rationale=" | ".join(
                    [decision.rationale for decision in actionable if decision.rationale]
                ),
                source="+".join(sorted({decision.source or "unknown" for decision in actionable})),
                confidence=preferred.confidence,
                leverage=preferred.leverage,
            )
            if len(actionable) > 1:
                merged_decision.order_type = "market"
                merged_decision.limit_price = None
            merged.append(merged_decision)

        return merged

    def build_ai_prompt(self, context: DecisionContext) -> str:
        payload = OrderedDict(context.to_prompt_payload())
        try:
            with open(self.prompt_log_path, "a", encoding="utf-8") as handle:
                handle.write(
                    f"\n\n--- {context.invocation.current_time.isoformat()} ---\n"
                    f"{json.dumps(payload, indent=2, default=json_default)}\n"
                )
        except Exception:
            pass
        logging.info(
            "Prompt length: %d chars for %d assets",
            len(json.dumps(payload, default=json_default)),
            len(context.assets),
        )
        return json.dumps(payload, default=json_default)

    async def run_strategies(
        self,
        strategy_contexts: list[tuple[Strategy, DecisionContext]],
    ) -> tuple[list[TradeIntent], list[str]]:
        all_source_decisions: list[TradeIntent] = []
        reasoning_chunks: list[str] = []
        for strategy, context in strategy_contexts:
            result: StrategyResult = await strategy.generate(context)
            if result.reasoning:
                reasoning_chunks.append(f"{result.source}: {result.reasoning[:1000]}")
            scaled = self.scale_decision_allocations(result.intents, context.capital_budget_usd)
            for intent in scaled:
                intent.source = result.source
            all_source_decisions.extend(scaled)
        return all_source_decisions, reasoning_chunks
