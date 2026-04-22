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

## Provider Notes

- Anthropic mode supports tool-calling (`fetch_indicator`) and optional thinking budget.
- OpenAI and Gemini currently run direct prompt-response path (tool-calling is Anthropic-only in this version).
- Output sanitization uses `AI_SANITIZE_MODEL` (or provider-specific default if unset).

## API Endpoints

When running, local API exposes:

- `GET /diary` -> trade diary JSON or raw output
- `GET /logs` -> log file tail/raw output

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

Current `Dockerfile` installs Anthropic dependencies by default.
If you run OpenAI or Gemini inside Docker, add their SDK dependencies to the image first.

## License / Disclaimer

Use at your own risk.
No guarantee of returns.
This code is provided as-is and has not been audited.
