"""Rule-based trading decision engine.

This module provides an alternative to LLM decisions using deterministic
indicator rules. It can run standalone or alongside AI model decisions.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

from src.config import Settings, get_settings


REQUIRED_ALGO_FUNCTION = "generate_trade_decisions"


class AlgoTradingAgent:
    """Create trading decisions from user algo.py or built-in fallback rules."""

    def __init__(
        self,
        algo_file_path: str | None = None,
        settings: Settings | None = None,
    ):
        self.settings = settings or get_settings()
        configured_path = algo_file_path or self.settings.execution.algo_file_path
        self.algo_file_path = configured_path
        self.custom_algo = self._load_custom_algo(configured_path)

    def decide_trade(
        self,
        assets: list[str],
        market_sections: list[dict],
        capital_budget_usd: float,
        account_snapshot: dict | None = None,
        invocation_context: dict | None = None,
    ) -> dict:
        """Return JSON-compatible decisions for all assets.

        The output schema mirrors the AI decision schema used by the main
        execution pipeline.
        """
        if self.custom_algo is not None:
            try:
                raw = self.custom_algo({
                    "assets": assets,
                    "market_data": market_sections,
                    "capital_budget_usd": capital_budget_usd,
                    "account": account_snapshot or {},
                    "invocation": invocation_context or {},
                })
                normalized = self._normalize_custom_output(raw, assets)
                return normalized
            except Exception as exc:
                logging.exception("Custom algo execution failed, using fallback rules: %s", exc)

        return self._decide_builtin(assets, market_sections, capital_budget_usd)

    def _load_custom_algo(self, algo_file_path: str):
        """Load a custom algorithm function from a local Python file."""
        path = self.settings.resolve_algo_path(Path.cwd(), algo_file_path=algo_file_path)
        if not path.exists():
            logging.info("Custom algo file not found at %s; using built-in algo", path)
            return None

        spec = importlib.util.spec_from_file_location("user_algo_module", str(path))
        if spec is None or spec.loader is None:
            logging.warning("Could not load custom algo module from %s; using built-in algo", path)
            return None

        try:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            logging.exception("Failed to import custom algo from %s; using built-in algo: %s", path, exc)
            return None

        fn = getattr(module, REQUIRED_ALGO_FUNCTION, None)
        if not callable(fn):
            logging.warning(
                "Custom algo missing required function '%s' in %s; using built-in algo",
                REQUIRED_ALGO_FUNCTION,
                path,
            )
            return None

        logging.info("Loaded custom algo from %s (%s)", path, REQUIRED_ALGO_FUNCTION)
        return fn

    def _normalize_custom_output(self, raw_output: Any, assets: list[str]) -> dict:
        """Normalize custom strategy output to the engine's expected schema."""
        reasoning = ""
        decisions_raw: list[dict] = []
        max_leverage = self.settings.risk.max_leverage

        if isinstance(raw_output, dict):
            reasoning = str(raw_output.get("reasoning") or "")
            maybe_decisions = raw_output.get("trade_decisions")
            if isinstance(maybe_decisions, list):
                decisions_raw = [d for d in maybe_decisions if isinstance(d, dict)]
        elif isinstance(raw_output, list):
            decisions_raw = [d for d in raw_output if isinstance(d, dict)]

        by_asset = {
            str(d.get("asset")): d
            for d in decisions_raw
            if d.get("asset") in assets
        }

        normalized: list[dict[str, Any]] = []
        for asset in assets:
            source = by_asset.get(asset, {})
            action = str(source.get("action") or "hold").lower()
            if action not in {"buy", "sell", "hold"}:
                action = "hold"

            alloc = _to_float(source.get("allocation_usd")) or 0.0
            if alloc < 0:
                alloc = 0.0

            order_type = str(source.get("order_type") or "market").lower()
            if order_type not in {"market", "limit"}:
                order_type = "market"

            limit_price = _to_float(source.get("limit_price"))
            if order_type == "market":
                limit_price = None

            confidence = _clamp_confidence(_to_float(source.get("confidence")))

            raw_leverage = _to_float(source.get("leverage"))
            if raw_leverage is None:
                raw_leverage = _to_float(source.get("requested_leverage"))
            leverage = None
            if raw_leverage is not None:
                leverage = min(max(raw_leverage, 1.0), max_leverage)
            elif confidence is not None and action in {"buy", "sell"}:
                leverage = _confidence_to_leverage(confidence, max_leverage)

            if action == "hold":
                if confidence is None:
                    confidence = 0.0
                leverage = 1.0

            decision = {
                "asset": asset,
                "action": action,
                "allocation_usd": round(float(alloc), 2),
                "order_type": order_type,
                "limit_price": limit_price,
                "tp_price": _to_float(source.get("tp_price")),
                "sl_price": _to_float(source.get("sl_price")),
                "exit_plan": str(source.get("exit_plan") or ""),
                "rationale": str(source.get("rationale") or "Custom algo decision"),
                "confidence": confidence,
                "leverage": round(float(leverage), 2) if leverage is not None else None,
            }
            normalized.append(decision)

        final_reasoning = reasoning or "Decisions provided by custom algo.py"
        return {"reasoning": final_reasoning, "trade_decisions": normalized}

    def _decide_builtin(self, assets: list[str], market_sections: list[dict], capital_budget_usd: float) -> dict:
        """Built-in deterministic fallback strategy."""
        section_by_asset = {
            str(section.get("asset")): section
            for section in market_sections
            if isinstance(section, dict) and section.get("asset")
        }

        decisions: list[dict[str, Any]] = []
        for asset in assets:
            section = section_by_asset.get(asset)
            decisions.append(self._decision_for_asset(asset, section))

        actionable = [
            d for d in decisions
            if d.get("action") in {"buy", "sell"}
        ]

        if capital_budget_usd <= 0:
            for d in actionable:
                d["action"] = "hold"
                d["allocation_usd"] = 0.0
                d["order_type"] = "market"
                d["limit_price"] = None
                d["tp_price"] = None
                d["sl_price"] = None
                d["exit_plan"] = ""
                d["rationale"] = f"{d.get('rationale', '')} Capital budget is 0 for algo mode.".strip()
            reasoning = "Algo mode enabled but capital budget is 0; all signals downgraded to hold."
            return {"reasoning": reasoning, "trade_decisions": decisions}

        if actionable:
            per_trade_alloc = capital_budget_usd / len(actionable)
            for d in actionable:
                d["allocation_usd"] = round(per_trade_alloc, 2)

        reasoning = (
            "Rule-based decisions generated using 4h trend (EMA20/EMA50), "
            "5m momentum (MACD/RSI), and ATR-based TP/SL."
        )
        return {"reasoning": reasoning, "trade_decisions": decisions}

    def _decision_for_asset(self, asset: str, section: dict | None) -> dict[str, Any]:
        """Build a single-asset decision from computed indicator snapshots."""
        max_leverage = self.settings.risk.max_leverage
        base = {
            "asset": asset,
            "action": "hold",
            "allocation_usd": 0.0,
            "order_type": "market",
            "limit_price": None,
            "tp_price": None,
            "sl_price": None,
            "exit_plan": "",
            "rationale": "No actionable signal.",
            "confidence": 0.0,
            "leverage": 1.0,
        }

        if not section:
            base["rationale"] = "No market section available for this asset."
            return base

        current_price = _to_float(section.get("current_price"))
        intraday = section.get("intraday") if isinstance(section.get("intraday"), dict) else {}
        long_term = section.get("long_term") if isinstance(section.get("long_term"), dict) else {}

        ema20_5m = _to_float(intraday.get("ema20"))
        macd_5m = _to_float(intraday.get("macd"))
        rsi14_5m = _to_float(intraday.get("rsi14"))
        ema20_4h = _to_float(long_term.get("ema20"))
        ema50_4h = _to_float(long_term.get("ema50"))
        atr14_4h = _to_float(long_term.get("atr14"))

        required = [current_price, ema20_5m, macd_5m, rsi14_5m, ema20_4h, ema50_4h]
        if any(v is None for v in required):
            base["rationale"] = "Insufficient indicator data for deterministic rule-set."
            return base

        trend_up = ema20_4h > ema50_4h and current_price > ema20_5m
        trend_down = ema20_4h < ema50_4h and current_price < ema20_5m
        momentum_up = macd_5m > 0 and 50 <= rsi14_5m <= 72
        momentum_down = macd_5m < 0 and 28 <= rsi14_5m <= 50

        atr_proxy = atr14_4h if (atr14_4h is not None and atr14_4h > 0) else max(current_price * 0.01, 0.01)

        if trend_up and momentum_up:
            confidence = 0.72
            base["action"] = "buy"
            base["tp_price"] = round(current_price + 1.5 * atr_proxy, 2)
            base["sl_price"] = round(current_price - 1.0 * atr_proxy, 2)
            base["exit_plan"] = "Close if 5m MACD turns negative or 4h EMA20 crosses below EMA50."
            base["rationale"] = "4h uptrend and 5m momentum alignment support a long entry."
            base["confidence"] = confidence
            base["leverage"] = _confidence_to_leverage(confidence, max_leverage)
            return base

        if trend_down and momentum_down:
            confidence = 0.72
            base["action"] = "sell"
            base["tp_price"] = round(current_price - 1.5 * atr_proxy, 2)
            base["sl_price"] = round(current_price + 1.0 * atr_proxy, 2)
            base["exit_plan"] = "Close if 5m MACD turns positive or 4h EMA20 crosses above EMA50."
            base["rationale"] = "4h downtrend and 5m momentum alignment support a short entry."
            base["confidence"] = confidence
            base["leverage"] = _confidence_to_leverage(confidence, max_leverage)
            return base

        base["rationale"] = "Trend and momentum are not aligned; holding to avoid churn."
        return base


def _to_float(value: Any) -> float | None:
    """Safely convert indicator values to float."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp_confidence(value: float | None) -> float | None:
    """Normalize confidence scores to the expected [0, 1] range."""
    if value is None:
        return None
    return round(min(max(value, 0.0), 1.0), 4)


def _confidence_to_leverage(confidence: float, max_leverage: float) -> float:
    """Map confidence [0,1] to leverage [1,max_leverage] with linear scaling."""
    bounded = min(max(confidence, 0.0), 1.0)
    lev = 1.0 + bounded * (max_leverage - 1.0)
    return round(lev, 2)
