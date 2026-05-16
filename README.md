# Hyperliquid AI Trading Agent

AI-driven and algo-capable trading loop for Hyperliquid perpetual markets.

Current AI providers (one active at a time):

- Anthropic
- OpenAI
- Gemini

The runtime also supports hybrid mode where AI and algo run together with a configurable capital split.

## Highlights

- Multi-provider AI decision engine with shared JSON output contract.
- Optional deterministic custom strategy from `algo.py`.
- Paper trading via `DRY_RUN=true`: real market data and AI decisions with local simulated fills.
- Local indicator stack (no external TA API requirement for core flow).
- Risk manager enforces hard checks before every execution.
- Market support includes standard and HIP-3 symbols (`dex:asset`, for example `xyz:GOLD`).
- Local observability endpoints and logs for cycle/debug inspection.

## Trade Sources and Merge Logic

Per cycle, enabled sources produce decisions:

1. AI source (selected by `AI_PROVIDER`)
2. Algo source (if enabled)

Then runtime merges per asset:

- same-direction actionable signals -> allocations are combined
- conflicting directions -> `hold` for that asset

## Safety and Risk Controls

All controls are enforced in code, not only in prompts.

Effective defaults (from config loader):

| Guard | Default | Behavior |
|---|---:|---|
| Max Position Size | 20% | Caps single-trade allocation vs account value |
| Force-Close Loss | 20% | Closes positions exceeding configured loss threshold |
| Max Leverage | 10x | Clamps requested leverage and blocks excess effective leverage |
| Max Total Exposure | 80% | Blocks trades that exceed total notional cap |
| Daily Circuit Breaker | 25% | Blocks new trades after daily drawdown breach |
| Mandatory Stop-Loss | 5% | Auto-sets SL when missing |
| Max Concurrent Positions | 10 | Limits simultaneous open positions |
| Minimum Balance Reserve | 10% | Prevents trading below reserve threshold |
| Minimum Trade Confidence | 0.55 | Blocks low-confidence actionable decisions when confidence provided |

Additional enforcement details:

- Minimum trade notional is effectively bumped to `$11` before execution.
- Risk manager can adjust allocations (for example, position-size capping) before placing orders.

## Setup

Use the profile-based setup guide:

- [setup.md](setup.md)

Use custom algo contract guide:

- [integration.md](integration.md)

## Quick Start

### 1) Install dependencies

With Poetry:

```bash
poetry install
```

With pip:

```bash
pip install hyperliquid-python-sdk anthropic openai google-genai python-dotenv aiohttp requests rich web3
```

### 2) Configure env

Copy and edit:

```bash
cp .env.example .env
```

Or use profile files (`.env.common` + one provider file) and generate `.env` (see [setup.md](setup.md)).

### 3) Run

```bash
python -m src.main
```

Or:

```bash
python -m src.main --assets BTC ETH SOL --interval 5m
```

## Key Environment Variables

Core runtime:

- `ASSETS`
- `INTERVAL`
- `DRY_RUN` (`true` simulates execution and never submits live orders)
- `DRY_RUN_INITIAL_BALANCE` (virtual USDC starting balance for paper trading)
- `HYPERLIQUID_PRIVATE_KEY`
- `HYPERLIQUID_VAULT_ADDRESS`

AI provider selection:

- `AI_PROVIDER` (`anthropic`, `openai`, `gemini`)
- `AI_MODEL`
- provider key matching selected provider:
  - `ANTHROPIC_API_KEY`
  - `OPENAI_API_KEY`
  - `GEMINI_API_KEY`

Execution modes:

- `ENABLE_AI_TRADING`
- `AI_CAPITAL_PCT`
- `ENABLE_ALGO_TRADING`
- `ALGO_CAPITAL_PCT`
- `ALGO_FILE_PATH`

Rules:

- each enabled percentage must be in `[0, 100]`
- enabled total must be `<= 100`
- at least one mode must be enabled

## Paper Trading

Set `DRY_RUN=true` to test the bot without live orders. In this mode the runtime still fetches real Hyperliquid market data, runs the configured AI/algo strategy, and applies the same risk manager checks. The broker wrapper simulates market fills at the latest fetched price, tracks a virtual portfolio in `dry_run_state.json`, keeps paper-only bookkeeping in `dry_run_active_trades.json` and `dry_run_risk_state.json`, and appends diary entries with `"dry_run": true`.

Dry-run trigger orders are also local: simulated stop-loss and take-profit orders close virtual positions when the latest price crosses the trigger.

## Provider Notes

- Anthropic mode uses the same pre-fetched indicator context as other providers and supports optional thinking budget.
- OpenAI, Anthropic, and Gemini run direct prompt-response paths without indicator tool-calling.
- Output sanitization uses `AI_SANITIZE_MODEL` (or provider-specific default if unset).

## API Endpoints

When running, local API exposes:

- `GET /diary` -> trade diary JSON or raw output
- `GET /alarms` -> critical trading alarms, including unprotected-position failures
- `GET /logs` -> log file tail/raw output

## Operational Scenarios

- AI provider/API is down: the decision layer logs the provider error and returns hold decisions, so no new entries are submitted for that cycle.
- Malformed AI output: the strategy retries once with a stricter JSON instruction, then falls back to holds if parsing still fails.
- Invalid `ASSETS`: startup preloads Hyperliquid metadata and fails fast with the invalid symbols before trading begins.
- Safe retail overrides: when `SAFE_RETAIL_MODE=true`, startup logs a warning listing any risk env vars being overridden and the effective caps.
- Partial or uncertain fills: local active-trade state is marked pending and startup/cycle reconciliation polls exchange order and position state before acting again.
- Bot crash mid-trade: `active_trades.json` and risk state are persisted under `TRADING_DATA_DIR`; on restart, startup reconciliation rebuilds local state from exchange truth before the trading loop resumes.
- Daily circuit breaker fires: new decisions are skipped and pending entry orders are cancelled; protective/reconciliation logic continues to run.
- Missing or invalid stop-loss after entry: reconciliation attempts to repair the SL; if repair fails, it submits a fail-closed market flatten and writes an alarm.

## Project Structure

```text
src/
  main.py
  config_loader.py
  risk_manager.py
  agent/
    decision_maker.py
    algo_decision_maker.py
  indicators/
    local_indicators.py
    taapi_client.py
  trading/
    hyperliquid_api.py
  utils/
    formatting.py
    prompt_utils.py
algo.py
setup.md
integration.md
docs/ARCHITECTURE.md
```

## Architecture

High-level architecture and runtime sequence:

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Agent Wallet Notes

1. Create an API/signing wallet in Hyperliquid settings.
2. Authorize signer for your funded vault/main wallet.
3. Set `HYPERLIQUID_PRIVATE_KEY` to signer key.
4. Set `HYPERLIQUID_VAULT_ADDRESS` to main wallet.

The signer executes trades on behalf of the vault wallet and should not hold treasury funds.

## Docker Note

The `Dockerfile` installs all supported AI provider SDKs and copies `algo.py`.
Use Compose for production so the bot restarts after crashes or host reboots and writes mutable state to `./data`:

```bash
docker compose up -d trading-bot
```

Enable 5-minute state archives, with optional rclone object-storage upload:

```bash
cp rclone.conf.example rclone.conf  # then fill in your remote
BACKUP_REMOTE=s3:my-bucket/hyperliquid-trading-agent docker compose --profile backup up -d
```

For one-off `docker run`, include `--restart=always`, `--env-file .env`, `-v "$PWD/data:/app/data"`, and `-v "$PWD/algo.py:/app/algo.py:ro"`.

## License / Disclaimer

Use at your own risk.
No guarantee of returns.
This code is provided as-is and has not been audited.
