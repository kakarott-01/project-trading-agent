# Custom Algo Integration

This project supports plug-and-play custom algorithm strategies through a plain Python file.

## Quick Start

1. Copy your strategy into algo.py at project root (or set a custom path).
2. Implement the required function shown below.
3. Enable algo mode in .env.
4. Run the bot normally.

## Required Function

Your strategy file MUST define this function:

generate_trade_decisions(context)

For a stable confidence-to-leverage contract across different trader algos,
you should also keep these helper functions in `algo.py`:

- calculate_confidence(asset_section, direction) -> float in [0, 1]
- confidence_to_leverage(confidence, max_leverage=10) -> float in [1, 10]

If this function is missing, the engine will log a warning and fall back to the built-in deterministic strategy.

## Function Input

context is a dictionary with:

- assets: list[str]
- market_data: list[dict]
- capital_budget_usd: float
- account: dict
- invocation: dict

market_data contains per-asset snapshots (current price, intraday indicators, long-term indicators, funding, OI).

## Function Output

Return either of these:

1) Dict format
- reasoning: str
- trade_decisions: list[dict]

2) List format
- trade_decisions only (list[dict])

Each trade_decisions item supports:

- asset: str
- action: buy | sell | hold
- allocation_usd: float
- order_type: market | limit
- limit_price: float | None
- tp_price: float | None
- sl_price: float | None
- exit_plan: str
- rationale: str
- confidence: float | None (recommended)
- leverage: float | None (recommended)

When confidence is supplied, low-confidence actionable trades can be blocked by
the risk manager using `MIN_TRADE_CONFIDENCE`.

Any missing asset is treated as hold.

## Env Configuration

Set in .env:

- ENABLE_ALGO_TRADING=true
- ALGO_CAPITAL_PCT=40
- ALGO_FILE_PATH=algo.py
- MIN_TRADE_CONFIDENCE=0.55

Optional dual-mode setup:

- ENABLE_AI_TRADING=true
- AI_CAPITAL_PCT=60
- AI_PROVIDER=openai (or anthropic/gemini)
- AI_MODEL=gpt-4.1 (or provider-specific model)

Rule: enabled capital percentages must each be in [0, 100], and enabled total must not exceed 100.

## Safety Notes

- Risk checks still run after your algorithm decisions.
- Invalid custom output is normalized where possible.
- If your file errors at runtime, the engine falls back to built-in algo logic.
