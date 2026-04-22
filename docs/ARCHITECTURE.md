## Trading Agent Architecture

This document describes how the current system works after the multi-provider AI update.
It focuses on runtime behavior, data contracts, and safety enforcement.

### 1) Core Subsystems

- Config Loader (`src/config_loader.py`)
  - Loads env values from `.env`.
  - Supports one active AI provider at a time via `AI_PROVIDER` and `AI_MODEL`.
  - Supports backward-compatible aliases (`ENABLE_CLAUDE_TRADING`, `CLAUDE_CAPITAL_PCT`, `LLM_MODEL`, `SANITIZE_MODEL`, `LLM_PROVIDER`).

- Runtime Orchestrator (`src/main.py`)
  - Parses CLI args or falls back to `ASSETS` and `INTERVAL` from env.
  - Validates execution modes and capital percentages.
  - Runs the recurring trading loop and local API server.

- Market/Execution Gateway (`src/trading/hyperliquid_api.py`)
  - Wraps Hyperliquid SDK with retry logic and client reset on transient failures.
  - Supports mainnet and testnet, standard and HIP-3 `dex:asset` symbols.
  - Handles order placement, leverage updates, TP/SL triggers, open orders, fills, and user state.

- Decision Engines
  - AI Engine (`src/agent/decision_maker.py`)
    - Providers: Anthropic, OpenAI, Gemini.
    - One active provider per run.
    - Anthropic path supports iterative tool-calling (`fetch_indicator`) and optional thinking budget.
    - OpenAI/Gemini run direct prompt-response (tool-calling currently Anthropic-only).
    - Normalizes malformed output via sanitizer model and falls back to hold decisions on hard failure.
  - Algo Engine (`src/agent/algo_decision_maker.py`)
    - Loads user strategy from `ALGO_FILE_PATH` if it exports `generate_trade_decisions(context)`.
    - Falls back to built-in deterministic strategy if file/function is missing or errors.

- Risk Engine (`src/risk_manager.py`)
  - Enforces all hard constraints before execution.
  - Can block or adjust proposed trades (for example, capping allocation, auto stop-loss).

- Local Indicators (`src/indicators/local_indicators.py`)
  - Computes indicators locally from Hyperliquid OHLCV data.
  - Indicator set includes EMA, SMA, RSI, MACD, ATR, Bollinger Bands, ADX, OBV, VWAP.

- Observability
  - API endpoints from `src/main.py`:
    - `GET /diary`
    - `GET /logs`
  - Local files:
    - `prompts.log`
    - `llm_requests.log`
    - `decisions.jsonl`
    - `diary.jsonl`

### 2) End-to-End Runtime Flow

Each cycle in `src/main.py` follows this sequence:

1. Refresh account/user state and current positions.
2. Apply force-close rule for positions exceeding max loss threshold.
3. Reconcile stale local active trades against exchange truth (positions + open orders).
4. Gather market context for all assets:
   - current mid price
   - open interest
   - funding rate
   - 5m and 4h candles
   - locally computed indicators
5. Run enabled decision sources:
   - AI source (if `ENABLE_AI_TRADING=true`)
   - Algo source (if `ENABLE_ALGO_TRADING=true`)
6. Scale each source's requested allocations to its capital budget.
7. Merge AI/algo outputs per asset:
   - same direction => combine allocations
   - conflicting directions => hold for that asset
8. For actionable trades, run risk validation and adjustments.
9. Set leverage on exchange, place order (market/limit), then attach TP/SL triggers when provided.
10. Persist cycle and trade logs.

### 3) Decision Data Contract

Internal normalized decision keys:

- `asset`
- `action` (`buy` | `sell` | `hold`)
- `allocation_usd`
- `order_type` (`market` | `limit`)
- `limit_price`
- `tp_price`
- `sl_price`
- `exit_plan`
- `rationale`
- optional `confidence`
- optional `leverage`

Missing or malformed AI outputs are sanitized where possible; otherwise the system emits hold decisions.

### 4) Risk Enforcement Order

For non-hold trades, validation runs in this order:

1. Confidence threshold (if confidence present).
2. Requested leverage clamped to `[1, MAX_LEVERAGE]`.
3. Allocation sanity and minimum order notional bump (minimum effectively enforced as `$11`).
4. Daily drawdown circuit breaker check.
5. Balance reserve check.
6. Position size cap (can reduce allocation instead of rejecting).
7. Total exposure cap.
8. Effective leverage check against account balance.
9. Concurrent position count limit.
10. Mandatory stop-loss auto-injection if missing.

### 5) AI Provider Model

Only one provider is active at a time:

- `AI_PROVIDER=anthropic`
- `AI_PROVIDER=openai`
- `AI_PROVIDER=gemini`

Behavior differences:

- Anthropic
  - Supports tool-calling loop (`fetch_indicator`) up to 6 tool turns.
  - Optional thinking controls (`THINKING_ENABLED`, `THINKING_BUDGET_TOKENS`).
- OpenAI and Gemini
  - Use direct text completion path.
  - If `ENABLE_TOOL_CALLING=true`, system logs that tool-calling is Anthropic-only and continues without tools.

### 6) Capital Allocation Model

The runtime validates and enforces:

- `AI_CAPITAL_PCT` in `[0, 100]`
- `ALGO_CAPITAL_PCT` in `[0, 100]`
- sum of enabled sources must be `<= 100`
- at least one source must be enabled

Per-source decisions are scaled to source budget before cross-source merge.

### 7) Failure Handling

- Hyperliquid calls use retry with exponential backoff and optional client re-init.
- AI responses are retried once in `main.py` if clearly invalid.
- Parsing/sanitization failures degrade to safe hold decisions.
- Strategy conflicts degrade to hold decisions.

Overall design principle: when uncertain, fail closed (hold), not open (trade).


