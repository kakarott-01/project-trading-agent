# Setup Guide

This project supports one active AI provider at a time:

- anthropic
- openai
- gemini

It also supports optional hybrid mode (AI + algo together).

## 1) Prerequisites

- Python 3.12+
- Hyperliquid API signer wallet
- Hyperliquid vault/main wallet address
- At least one provider API key

## 2) Install dependencies

Option A (Poetry, recommended):

```bash
poetry install
```

Option B (pip):

```bash
pip install \
	hyperliquid-python-sdk \
	anthropic \
	openai \
	google-genai \
	python-dotenv \
	aiohttp \
	requests \
	rich \
	web3
```

## 3) Create env profile files

Create these files at project root:

- `.env.common`
- `.env.anthropic`
- `.env.openai`
- `.env.gemini`
- `.env` (generated runtime file)

The app reads only `.env` at runtime.

## 4) Fill `.env.common` (shared settings)

Use provider-agnostic keys here.

```env
# Hyperliquid
HYPERLIQUID_PRIVATE_KEY=0x_your_agent_private_key
HYPERLIQUID_VAULT_ADDRESS=0x_your_main_wallet
HYPERLIQUID_NETWORK=mainnet

# Runtime
ASSETS="BTC ETH SOL OIL GOLD SILVER SPX"
INTERVAL="5m"

# Execution modes
ENABLE_AI_TRADING=true
AI_CAPITAL_PCT=100
ENABLE_ALGO_TRADING=false
ALGO_CAPITAL_PCT=0
ALGO_FILE_PATH=algo.py

# Risk
MAX_POSITION_PCT=20
MAX_LOSS_PER_POSITION_PCT=20
MAX_LEVERAGE=10
MAX_TOTAL_EXPOSURE_PCT=80
DAILY_LOSS_CIRCUIT_BREAKER_PCT=25
MANDATORY_SL_PCT=5
MAX_CONCURRENT_POSITIONS=10
MIN_BALANCE_RESERVE_PCT=10
MIN_TRADE_CONFIDENCE=0.55

# AI runtime options
MAX_TOKENS=4096
ENABLE_TOOL_CALLING=false

# API server
API_HOST=0.0.0.0
API_PORT=3000
```

## 5) Fill provider profile files

### `.env.anthropic`

```env
AI_PROVIDER=anthropic
AI_MODEL=claude-sonnet-4-20250514
ANTHROPIC_API_KEY=your_anthropic_api_key_here

# Optional
AI_SANITIZE_MODEL=claude-haiku-4-5-20251001
THINKING_ENABLED=false
THINKING_BUDGET_TOKENS=10000
```

### `.env.openai`

```env
AI_PROVIDER=openai
AI_MODEL=gpt-4.1
OPENAI_API_KEY=your_openai_api_key_here

# Optional
OPENAI_BASE_URL=https://api.openai.com/v1
AI_SANITIZE_MODEL=gpt-4.1-mini
```

### `.env.gemini`

```env
AI_PROVIDER=gemini
AI_MODEL=gemini-2.5-pro
GEMINI_API_KEY=your_gemini_api_key_here

# Optional
AI_SANITIZE_MODEL=gemini-2.5-flash
```

## 6) Build runtime `.env`

From project root, combine common + one provider profile.

Use Anthropic:

```bash
cat .env.common .env.anthropic > .env
```

Use OpenAI:

```bash
cat .env.common .env.openai > .env
```

Use Gemini:

```bash
cat .env.common .env.gemini > .env
```

## 7) Validate selected provider

```bash
grep '^AI_PROVIDER=' .env
grep '^AI_MODEL=' .env
```

## 8) Run the bot

```bash
python -m src.main
```

Or with explicit CLI params:

```bash
python -m src.main --assets BTC ETH SOL --interval 5m
```

## 9) Hybrid mode (AI + algo)

In `.env.common`:

```env
ENABLE_AI_TRADING=true
ENABLE_ALGO_TRADING=true
AI_CAPITAL_PCT=60
ALGO_CAPITAL_PCT=40
```

Rules enforced at startup:

- each enabled capital percentage must be in `[0, 100]`
- enabled total must be `<= 100`
- at least one mode must be enabled

## 10) Optional zsh aliases

```bash
alias use_anthropic='cat .env.common .env.anthropic > .env && echo Active profile: anthropic'
alias use_openai='cat .env.common .env.openai > .env && echo Active profile: openai'
alias use_gemini='cat .env.common .env.gemini > .env && echo Active profile: gemini'
```

## 11) Security and git hygiene

- Do not commit secrets.
- Keep `.env` and private profile files ignored by git.
- Rotate keys immediately if exposed.
