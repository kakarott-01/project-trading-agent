"""AI strategy adapter around the multi-provider LLM decision engine."""

from __future__ import annotations

import json
import logging
import asyncio

from src.agent.decision_maker import LLMDecisionEngine
from src.config import Settings
from src.domain.models import DecisionContext, StrategyResult, TradeIntent
from src.exchanges.base import MarketDataPort
from src.strategies.base import Strategy
from src.strategies.executors import AI_EXECUTOR
from src.utils.prompt_utils import json_default


class AIStrategy(Strategy):
    """Runs the configured AI provider behind the common strategy interface."""

    def __init__(self, settings: Settings, broker: MarketDataPort, prompt_builder):
        self.settings = settings
        self.source = f"ai:{settings.ai.provider}"
        self.agent = LLMDecisionEngine(hyperliquid=broker, settings=settings)
        self.prompt_builder = prompt_builder

    async def generate(self, context: DecisionContext) -> StrategyResult:
        prompt = self.prompt_builder.build_ai_prompt(context)

        def _is_failed_outputs(outs) -> bool:
            if not isinstance(outs, dict):
                return True
            decisions = outs.get("trade_decisions")
            if not isinstance(decisions, list) or not decisions:
                return True
            return any(
                isinstance(decision, dict)
                and decision.get("action") == "hold"
                and decision.get("rationale") == "Parse error"
                for decision in decisions
            )

        try:
            loop = asyncio.get_running_loop()
            outputs = await loop.run_in_executor(
                AI_EXECUTOR,
                self.agent.decide_trade,
                context.assets,
                prompt,
            )
        except Exception as exc:
            logging.error("Agent error: %s", exc)
            outputs = {}

        if _is_failed_outputs(outputs):
            logging.warning("Retrying AI once due to invalid/parse-error output")
            retry_payload = {
                "retry_instruction": "Return ONLY the JSON object per schema, no prose.",
                "original_context": context.to_prompt_payload(),
            }
            try:
                loop = asyncio.get_running_loop()
                outputs = await loop.run_in_executor(
                    AI_EXECUTOR,
                    self.agent.decide_trade,
                    context.assets,
                    json.dumps(retry_payload, default=json_default),
                )
            except Exception as exc:
                logging.error("Retry agent error: %s", exc)
                outputs = {}

        reasoning_text = outputs.get("reasoning", "") if isinstance(outputs, dict) else ""
        decisions = outputs.get("trade_decisions", []) if isinstance(outputs, dict) else []
        return StrategyResult(
            source=self.source,
            reasoning=reasoning_text,
            intents=[TradeIntent.from_dict({**decision, "source": self.source}) for decision in decisions],
        )
