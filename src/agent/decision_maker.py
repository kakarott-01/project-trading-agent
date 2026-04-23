"""Decision-making agent that orchestrates LLM prompts and indicator lookups.

Supports one active provider at a time: Anthropic, OpenAI, or Gemini.
"""

import asyncio
import json
import logging
from datetime import datetime

from src.config import Settings, get_settings
from src.indicators.local_indicators import compute_all, last_n, latest


class TradingAgent:
    """High-level trading agent that delegates reasoning to the configured AI model."""

    SUPPORTED_PROVIDERS = {"anthropic", "openai", "gemini"}

    def __init__(self, hyperliquid=None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.provider = self.settings.ai.provider
        if self.provider not in self.SUPPORTED_PROVIDERS:
            raise ValueError(
                f"Unsupported AI provider '{self.provider}'. Supported: {sorted(self.SUPPORTED_PROVIDERS)}"
            )

        self.model = self.settings.ai.model
        self.sanitize_model = self.settings.ai.sanitize_model or self.model
        self.max_tokens = self.settings.ai.max_tokens
        self.enable_tool_calling = self.settings.ai.enable_tool_calling
        self.hyperliquid = hyperliquid

        self.anthropic_client = None
        self.openai_client = None
        self.gemini_client = None

        if self.provider == "anthropic":
            self.anthropic_client = self._build_anthropic_client(
                self.settings.ai.anthropic_api_key
            )
        elif self.provider == "openai":
            self.openai_client = self._build_openai_client(
                self.settings.ai.openai_api_key,
                self.settings.ai.openai_base_url,
            )
        elif self.provider == "gemini":
            self.gemini_client = self._build_gemini_client(
                self.settings.ai.gemini_api_key
            )

    @staticmethod
    def _build_anthropic_client(api_key):
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required when AI_PROVIDER=anthropic")
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError("anthropic package is required for AI_PROVIDER=anthropic") from exc
        return anthropic.Anthropic(api_key=api_key)

    @staticmethod
    def _build_openai_client(api_key, base_url=None):
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required when AI_PROVIDER=openai")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai package is required for AI_PROVIDER=openai") from exc
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        return OpenAI(**kwargs)

    @staticmethod
    def _build_gemini_client(api_key):
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is required when AI_PROVIDER=gemini")
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError("google-genai package is required for AI_PROVIDER=gemini") from exc
        return genai.Client(api_key=api_key)

    def decide_trade(self, assets, context):
        """Decide for multiple assets in one call."""
        return self._decide(context, assets=assets)

    @staticmethod
    def _strip_code_fences(raw_text: str) -> str:
        cleaned = (raw_text or "").strip()
        if cleaned.startswith("```"):
            first_newline = cleaned.find("\n")
            if first_newline != -1:
                cleaned = cleaned[first_newline + 1 :]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()
        return cleaned

    @staticmethod
    def _fallback_hold(assets, reason: str):
        return {
            "reasoning": reason,
            "trade_decisions": [
                {
                    "asset": a,
                    "action": "hold",
                    "allocation_usd": 0.0,
                    "order_type": "market",
                    "limit_price": None,
                    "tp_price": None,
                    "sl_price": None,
                    "exit_plan": "",
                    "rationale": reason,
                }
                for a in assets
            ],
        }

    @staticmethod
    def _normalize_trade_output(parsed, assets):
        if not isinstance(parsed, dict):
            return {"reasoning": "", "trade_decisions": []}

        reasoning_text = parsed.get("reasoning", "") or ""
        decisions = parsed.get("trade_decisions")
        if not isinstance(decisions, list):
            return {"reasoning": reasoning_text, "trade_decisions": []}

        normalized = []
        for item in decisions:
            if not isinstance(item, dict):
                continue
            asset = item.get("asset")
            if asset not in assets:
                continue
            item = dict(item)
            item.setdefault("action", "hold")
            item.setdefault("allocation_usd", 0.0)
            item.setdefault("order_type", "market")
            item.setdefault("limit_price", None)
            item.setdefault("tp_price", None)
            item.setdefault("sl_price", None)
            item.setdefault("exit_plan", "")
            item.setdefault("rationale", "")
            normalized.append(item)

        return {"reasoning": reasoning_text, "trade_decisions": normalized}

    def _call_openai_text(self, system_prompt: str, user_content: str, model_override: str | None = None) -> str:
        model = model_override or self.model
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0,
        }
        try:
            response = self.openai_client.chat.completions.create(
                **kwargs,
                response_format={"type": "json_object"},
            )
        except Exception:
            response = self.openai_client.chat.completions.create(**kwargs)

        content = ""
        choices = getattr(response, "choices", []) or []
        if choices and getattr(choices[0], "message", None):
            content = choices[0].message.content or ""
        return content

    def _call_gemini_text(self, system_prompt: str, user_content: str, model_override: str | None = None) -> str:
        model = model_override or self.model
        prompt = (
            f"SYSTEM INSTRUCTIONS:\n{system_prompt}\n\n"
            f"USER INPUT:\n{user_content}\n\n"
            "Return strict JSON only."
        )
        try:
            from google.genai import types

            response = self.gemini_client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(max_output_tokens=self.max_tokens),
            )
        except Exception:
            response = self.gemini_client.models.generate_content(model=model, contents=prompt)
        return getattr(response, "text", "") or ""

    def _sanitize_output(self, raw_content: str, assets_list):
        """Use configured sanitize model to normalize malformed output."""
        normalizer_system = (
            "You are a strict JSON normalizer. Return ONLY a JSON object with two keys: "
            "\"reasoning\" (string) and \"trade_decisions\" (array). "
            "Each trade_decisions item must have: asset, action (buy/sell/hold), "
            "allocation_usd (number), order_type (\"market\" or \"limit\"), "
            "limit_price (number or null), tp_price (number or null), sl_price (number or null), "
            "exit_plan (string), rationale (string). "
            f"Valid assets: {json.dumps(list(assets_list))}. "
            "If input is wrapped in markdown or has prose, extract just the JSON. Do not add fields."
        )

        try:
            if self.provider == "anthropic":
                response = self.anthropic_client.messages.create(
                    model=self.sanitize_model,
                    max_tokens=2048,
                    system=normalizer_system,
                    messages=[{"role": "user", "content": raw_content}],
                )
                content = "".join(block.text for block in response.content if block.type == "text")
            elif self.provider == "openai":
                content = self._call_openai_text(normalizer_system, raw_content, model_override=self.sanitize_model)
            else:
                content = self._call_gemini_text(normalizer_system, raw_content, model_override=self.sanitize_model)

            parsed = json.loads(self._strip_code_fences(content))
            normalized = self._normalize_trade_output(parsed, assets_list)
            if normalized.get("trade_decisions"):
                return normalized
            return {"reasoning": "", "trade_decisions": []}
        except Exception as ex:
            logging.error("Sanitize failed: %s", ex)
            return {"reasoning": "", "trade_decisions": []}

    def _parse_response_text(self, raw_text: str, assets):
        if not raw_text.strip():
            return self._fallback_hold(assets, "Empty AI response")

        cleaned = self._strip_code_fences(raw_text)
        try:
            parsed = json.loads(cleaned)
            normalized = self._normalize_trade_output(parsed, assets)
            if normalized.get("trade_decisions") or normalized.get("reasoning"):
                return normalized

            logging.error("trade_decisions missing or invalid; attempting sanitize")
            sanitized = self._sanitize_output(raw_text, assets)
            if sanitized.get("trade_decisions"):
                return sanitized
            return normalized
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as ex:
            logging.error("JSON parse error: %s, content: %s", ex, raw_text[:200])
            sanitized = self._sanitize_output(raw_text, assets)
            if sanitized.get("trade_decisions"):
                return sanitized
            return self._fallback_hold(assets, "Parse error")

    def _decide(self, context, assets):
        """Dispatch decision request to configured provider and enforce output contract."""
        system_prompt = (
            "You are a rigorous QUANTITATIVE TRADER and interdisciplinary MATHEMATICIAN-ENGINEER optimizing risk-adjusted returns for perpetual futures under real execution, margin, and funding constraints.\n"
            "You will receive market + account context for SEVERAL assets, including:\n"
            f"- assets = {json.dumps(list(assets))}\n"
            "- per-asset intraday (5m) and higher-timeframe (4h) metrics\n"
            "- Active Trades with Exit Plans\n"
            "- Recent Trading History\n"
            "- Risk management limits (hard-enforced by the system, not just guidelines)\n\n"
            "Always use the 'current time' provided in the user message to evaluate any time-based conditions, such as cooldown expirations or timed exit plans.\n\n"
            "Your goal: make decisive, first-principles decisions per asset that minimize churn while capturing edge.\n\n"
            "Aggressively pursue setups where calculated risk is outweighed by expected edge; size positions so downside is controlled while upside remains meaningful.\n\n"
            "Core policy (low-churn, position-aware)\n"
            "1) Respect prior plans: If an active trade has an exit_plan with explicit invalidation (e.g., \"close if 4h close above EMA50\"), DO NOT close or flip early unless that invalidation (or a stronger one) has occurred.\n"
            "2) Hysteresis: Require stronger evidence to CHANGE a decision than to keep it. Only flip direction if BOTH:\n"
            "   a) Higher-timeframe structure supports the new direction (e.g., 4h EMA20 vs EMA50 and/or MACD regime), AND\n"
            "   b) Intraday structure confirms with a decisive break beyond ~0.5×ATR (recent) and momentum alignment (MACD or RSI slope).\n"
            "   Otherwise, prefer HOLD or adjust TP/SL.\n"
            "3) Cooldown: After opening, adding, reducing, or flipping, impose a self-cooldown of at least 3 bars of the decision timeframe (e.g., 3×5m = 15m) before another direction change, unless a hard invalidation occurs. Encode this in exit_plan (e.g., \"cooldown_bars:3 until 2025-10-19T15:55Z\"). You must honor your own cooldowns on future cycles.\n"
            "4) Funding is a tilt, not a trigger: Do NOT open/close/flip solely due to funding unless expected funding over your intended holding horizon meaningfully exceeds expected edge (e.g., > ~0.25×ATR). Consider that funding accrues discretely and slowly relative to 5m bars.\n"
            "5) Overbought/oversold != reversal by itself: Treat RSI extremes as risk-of-pullback. You need structure + momentum confirmation to bet against trend. Prefer tightening stops or taking partial profits over instant flips.\n"
            "6) Prefer adjustments over exits: If the thesis weakens but is not invalidated, first consider: tighten stop (e.g., to a recent swing or ATR multiple), trail TP, or reduce size. Flip only on hard invalidation + fresh confluence.\n\n"
            "Decision discipline (per asset)\n"
            "- Choose one: buy / sell / hold.\n"
            "- Proactively harvest profits when price action presents a clear, high-quality opportunity that aligns with your thesis.\n"
            "- You control allocation_usd (but the system will cap it - see risk limits below).\n"
            "- Order type: set order_type to \"market\" for immediate execution, or \"limit\" for resting orders.\n"
            "  - For limit orders, you MUST set limit_price. Use limit orders when you want better entry prices (e.g., buying a dip, selling a bounce).\n"
            "  - For market orders, limit_price should be null.\n"
            "  - Default is \"market\" if omitted.\n"
            "- TP/SL sanity:\n"
            "  - BUY: tp_price > current_price, sl_price < current_price\n"
            "  - SELL: tp_price < current_price, sl_price > current_price\n"
            "  If sensible TP/SL cannot be set, use null and explain the logic. A mandatory SL will be auto-applied if you do not set one.\n"
            "- exit_plan must include at least ONE explicit invalidation trigger and may include cooldown guidance you will follow later.\n\n"
            "Leverage policy (perpetual futures)\n"
            "- You can use leverage, but the system enforces a hard cap. Stay within the limits.\n"
            "- In high volatility (elevated ATR) or during funding spikes, reduce or avoid leverage.\n"
            "- Treat allocation_usd as notional exposure; keep it consistent with safe leverage and available margin.\n\n"
            "Tool usage\n"
            "- Use the fetch_indicator tool whenever an additional datapoint could sharpen your thesis; parameters: indicator (ema/sma/rsi/macd/bbands/atr/adx/obv/vwap/stoch_rsi/all), asset (e.g. \"BTC\", \"OIL\", \"GOLD\"), interval (\"5m\"/\"4h\"), optional period.\n"
            "- Indicators are computed locally from Hyperliquid candle data - works for ALL perp markets (crypto, commodities, indices).\n"
            "- Incorporate tool findings into your reasoning, but NEVER paste raw tool responses into the final JSON - summarize the insight instead.\n"
            "- Use tools to upgrade your analysis; lack of confidence is a cue to query them before deciding.\n\n"
            "Reasoning recipe (first principles)\n"
            "- Structure (trend, EMAs slope/cross, HH/HL vs LH/LL), Momentum (MACD regime, RSI slope), Liquidity/volatility (ATR, volume), Positioning tilt (funding, OI).\n"
            "- Favor alignment across 4h and 5m. Counter-trend scalps require stronger intraday confirmation and tighter risk.\n\n"
            "Output contract\n"
            "- Output ONLY a strict JSON object (no markdown, no code fences) with exactly two properties:\n"
            "  - \"reasoning\": long-form string capturing detailed, step-by-step analysis.\n"
            "  - \"trade_decisions\": array ordered to match the provided assets list.\n"
            "- Each item inside trade_decisions must contain the keys: asset, action, allocation_usd, order_type, limit_price, tp_price, sl_price, exit_plan, rationale.\n"
            "  - order_type: \"market\" (default) or \"limit\"\n"
            "  - limit_price: required if order_type is \"limit\", null otherwise\n"
            "- Do not emit Markdown or any extra properties.\n"
        )

        tools = [
            {
                "name": "fetch_indicator",
                "description": (
                    "Fetch technical indicators computed locally from Hyperliquid candle data. "
                    "Works for ALL Hyperliquid perp markets including crypto (BTC, ETH, SOL), "
                    "commodities (OIL, GOLD, SILVER), indices (SPX), and more. "
                    "Available indicators: ema, sma, rsi, macd, bbands, atr, adx, obv, vwap, stoch_rsi, all. "
                    "Returns the latest values and recent series."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "indicator": {
                            "type": "string",
                            "enum": [
                                "ema",
                                "sma",
                                "rsi",
                                "macd",
                                "bbands",
                                "atr",
                                "adx",
                                "obv",
                                "vwap",
                                "stoch_rsi",
                                "all",
                            ],
                        },
                        "asset": {
                            "type": "string",
                            "description": "Hyperliquid asset symbol, e.g. BTC, ETH, OIL, GOLD, SPX",
                        },
                        "interval": {
                            "type": "string",
                            "enum": ["1m", "5m", "15m", "1h", "4h", "1d"],
                        },
                        "period": {
                            "type": "integer",
                            "description": "Indicator period (default varies by indicator)",
                        },
                    },
                    "required": ["indicator", "asset", "interval"],
                },
            }
        ]

        messages = [{"role": "user", "content": context}]

        def _log_request(model, messages_to_log):
            with open("llm_requests.log", "a", encoding="utf-8") as f:
                f.write(f"\n\n=== {datetime.now()} ===\n")
                f.write(f"Provider: {self.provider}\n")
                f.write(f"Model: {model}\n")
                f.write(f"Messages count: {len(messages_to_log)}\n")
                last = messages_to_log[-1]
                content_str = str(last.get("content", ""))[:500]
                f.write(f"Last message role: {last.get('role')}\n")
                f.write(f"Last message content (truncated): {content_str}\n")

        def _call_anthropic(msgs, use_tools=True):
            _log_request(self.model, msgs)
            kwargs = {
                "model": self.model,
                "max_tokens": self.max_tokens,
                "system": system_prompt,
                "messages": msgs,
            }
            if use_tools and self.enable_tool_calling:
                kwargs["tools"] = tools
            if self.settings.ai.thinking_enabled:
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.settings.ai.thinking_budget_tokens,
                }
                kwargs["max_tokens"] = max(self.max_tokens, 16000)

            response = self.anthropic_client.messages.create(**kwargs)
            logging.info(
                "AI response (provider=%s): stop_reason=%s, usage=%s",
                self.provider,
                response.stop_reason,
                response.usage,
            )
            with open("llm_requests.log", "a", encoding="utf-8") as f:
                f.write(f"Response stop_reason: {response.stop_reason}\n")
                f.write(
                    f"Usage: input={response.usage.input_tokens}, output={response.usage.output_tokens}\n"
                )
            return response

        def _handle_tool_call(tool_name, tool_input):
            if tool_name != "fetch_indicator":
                return json.dumps({"error": f"Unknown tool: {tool_name}"})

            if self.hyperliquid is None:
                return json.dumps({"error": "Tool call unavailable: hyperliquid client is not set"})

            try:
                asset = tool_input["asset"]
                interval = tool_input["interval"]
                indicator = tool_input["indicator"]

                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        candles = pool.submit(
                            asyncio.run,
                            self.hyperliquid.get_candles(asset, interval, 100),
                        ).result(timeout=30)
                else:
                    candles = asyncio.run(self.hyperliquid.get_candles(asset, interval, 100))

                all_indicators = compute_all(candles)

                if indicator == "all":
                    result = {
                        k: {
                            "latest": latest(v) if isinstance(v, list) else v,
                            "series": last_n(v, 10) if isinstance(v, list) else v,
                        }
                        for k, v in all_indicators.items()
                    }
                elif indicator == "macd":
                    result = {
                        "macd": {
                            "latest": latest(all_indicators.get("macd", [])),
                            "series": last_n(all_indicators.get("macd", []), 10),
                        },
                        "signal": {
                            "latest": latest(all_indicators.get("macd_signal", [])),
                            "series": last_n(all_indicators.get("macd_signal", []), 10),
                        },
                        "histogram": {
                            "latest": latest(all_indicators.get("macd_histogram", [])),
                            "series": last_n(all_indicators.get("macd_histogram", []), 10),
                        },
                    }
                elif indicator == "bbands":
                    result = {
                        "upper": {
                            "latest": latest(all_indicators.get("bbands_upper", [])),
                            "series": last_n(all_indicators.get("bbands_upper", []), 10),
                        },
                        "middle": {
                            "latest": latest(all_indicators.get("bbands_middle", [])),
                            "series": last_n(all_indicators.get("bbands_middle", []), 10),
                        },
                        "lower": {
                            "latest": latest(all_indicators.get("bbands_lower", [])),
                            "series": last_n(all_indicators.get("bbands_lower", []), 10),
                        },
                    }
                elif indicator in ("ema", "sma"):
                    period = tool_input.get("period", 20)
                    from src.indicators.local_indicators import ema as _ema, sma as _sma

                    closes = [c["close"] for c in candles]
                    series = _ema(closes, period) if indicator == "ema" else _sma(closes, period)
                    result = {"latest": latest(series), "series": last_n(series, 10), "period": period}
                elif indicator == "rsi":
                    period = tool_input.get("period", 14)
                    from src.indicators.local_indicators import rsi as _rsi

                    series = _rsi(candles, period)
                    result = {"latest": latest(series), "series": last_n(series, 10), "period": period}
                elif indicator == "atr":
                    period = tool_input.get("period", 14)
                    from src.indicators.local_indicators import atr as _atr

                    series = _atr(candles, period)
                    result = {"latest": latest(series), "series": last_n(series, 10), "period": period}
                else:
                    key_map = {
                        "adx": "adx",
                        "obv": "obv",
                        "vwap": "vwap",
                        "stoch_rsi": "stoch_rsi",
                    }
                    mapped = key_map.get(indicator, indicator)
                    series = all_indicators.get(mapped, [])
                    result = {
                        "latest": latest(series) if isinstance(series, list) else series,
                        "series": last_n(series, 10) if isinstance(series, list) else series,
                    }

                return json.dumps(result, default=str)
            except Exception as ex:
                logging.error("Tool call error: %s", ex)
                return json.dumps({"error": str(ex)})

        if self.provider == "anthropic":
            for _ in range(6):
                try:
                    response = _call_anthropic(messages)
                except Exception as ex:
                    logging.error("Anthropic API error: %s", ex)
                    with open("llm_requests.log", "a", encoding="utf-8") as f:
                        f.write(f"API Error: {ex}\n")
                    break

                tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
                text_blocks = [b for b in response.content if b.type == "text"]

                if tool_use_blocks and response.stop_reason == "tool_use":
                    assistant_content = []
                    for block in response.content:
                        if block.type == "text":
                            assistant_content.append({"type": "text", "text": block.text})
                        elif block.type == "tool_use":
                            assistant_content.append(
                                {
                                    "type": "tool_use",
                                    "id": block.id,
                                    "name": block.name,
                                    "input": block.input,
                                }
                            )
                        elif block.type == "thinking":
                            assistant_content.append({"type": "thinking", "thinking": block.thinking})
                    messages.append({"role": "assistant", "content": assistant_content})

                    tool_results = []
                    for block in tool_use_blocks:
                        result_str = _handle_tool_call(block.name, block.input)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_str,
                            }
                        )
                    messages.append({"role": "user", "content": tool_results})
                    continue

                raw_text = "".join(block.text for block in text_blocks)
                if not raw_text.strip():
                    logging.error("Empty response from provider=%s", self.provider)
                    break
                return self._parse_response_text(raw_text, assets)

            return self._fallback_hold(assets, "tool loop cap")

        if self.enable_tool_calling:
            logging.info(
                "ENABLE_TOOL_CALLING is only supported for AI_PROVIDER=anthropic in this version. "
                "Continuing without tool-calling for provider=%s",
                self.provider,
            )

        try:
            if self.provider == "openai":
                raw_text = self._call_openai_text(system_prompt, context)
            else:
                raw_text = self._call_gemini_text(system_prompt, context)
        except Exception as ex:
            logging.error("Provider call failed (provider=%s): %s", self.provider, ex)
            return self._fallback_hold(assets, f"Provider call failed: {ex}")

        return self._parse_response_text(raw_text, assets)
