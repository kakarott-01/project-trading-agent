# Custom Algo Integration

This project supports plug-and-play custom strategy logic through a Python file (default: `algo.py`).

## 1) Quick Start

1. Create or edit `algo.py` at project root (or point to another file with `ALGO_FILE_PATH`).
2. Implement `generate_trade_decisions(context)`.
3. Set `ENABLE_ALGO_TRADING=true`.
4. Run the bot.

If custom loading/execution fails, the runtime falls back to built-in deterministic rules.

## 2) Required Function

Your strategy file must export:

`generate_trade_decisions(context)`

Input: one dictionary. Output: either dict or list (details below).

## 3) Input Contract

`context` contains:

- `assets`: list of symbols requested this cycle
- `market_data`: per-asset snapshots from local indicator pipeline
- `capital_budget_usd`: budget allocated to algo source for this cycle
- `account`: account/dashboard snapshot
- `invocation`: cycle metadata

Current invocation keys include:

- `cycle`
- `current_time` (UTC ISO string)
- `interval`

## 4) Output Contract

You can return either:

1. Dict form
	- `reasoning`: string
	- `trade_decisions`: list of decisions
2. List form
	- list of decisions directly

Decision fields supported:

- `asset` (required)
- `action`: `buy` | `sell` | `hold`
- `allocation_usd`
- `order_type`: `market` | `limit`
- `limit_price`
- `tp_price`
- `sl_price`
- `exit_plan`
- `rationale`
- `confidence` (recommended)
- `leverage` or `requested_leverage` (recommended)

Normalization behavior performed by runtime:

- invalid/missing action -> `hold`
- invalid order type -> `market`
- market orders force `limit_price=null`
- missing assets in your output become `hold`

## 5) Runtime Behavior After Algo Output

After your function returns, the system still applies:

1. Source budget scaling (`capital_budget_usd`) so actionable notional fits budget.
2. Source merge logic (if AI is also enabled):
	- same-direction signals combine
	- direction conflicts become `hold` for that asset
3. Full risk validation before execution:
	- confidence threshold if provided (`MIN_TRADE_CONFIDENCE`)
	- leverage clamped to `[1, MAX_LEVERAGE]`
	- mandatory SL auto-set if missing
	- exposure, drawdown, reserve, and concurrency checks

Important: risk checks are authoritative. Algo output cannot bypass them.

## 6) Environment Configuration

Minimum algo-only setup:

```env
ENABLE_AI_TRADING=false
ENABLE_ALGO_TRADING=true
ALGO_CAPITAL_PCT=100
ALGO_FILE_PATH=algo.py
```

Hybrid setup example:

```env
ENABLE_AI_TRADING=true
AI_CAPITAL_PCT=60
ENABLE_ALGO_TRADING=true
ALGO_CAPITAL_PCT=40
```

Rules:

- each enabled capital percentage must be in `[0, 100]`
- enabled total must be `<= 100`
- at least one mode must be enabled

## 7) Recommended Pattern

Use confidence + leverage in output for tighter control:

- `confidence` in `[0, 1]`
- `leverage` in `[1, MAX_LEVERAGE]`

The default `algo.py` template already demonstrates this pattern with:

- `calculate_confidence(...)`
- `confidence_to_leverage(...)`

## 8) Minimal Example

```python
def generate_trade_decisions(context):
	 decisions = []
	 for asset in context.get("assets", []):
		  decisions.append({
				"asset": asset,
				"action": "hold",
				"allocation_usd": 0.0,
				"order_type": "market",
				"limit_price": None,
				"tp_price": None,
				"sl_price": None,
				"exit_plan": "",
				"rationale": "No setup this cycle.",
				"confidence": 0.0,
				"leverage": 1.0,
		  })
	 return {"reasoning": "Conservative hold strategy.", "trade_decisions": decisions}
```
