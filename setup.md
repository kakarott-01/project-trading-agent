# Environment Setup Guide

This guide shows a clean profile-based setup for all supported AI providers.

Supported providers:
- anthropic
- openai
- gemini

The app reads from one file named .env at runtime.
To keep things organized, you will maintain separate files and then build .env from them.

## 1) Create environment files

Create these files in the project root:
- .env.common
- .env.anthropic
- .env.openai
- .env.gemini
- .env (generated file used by the app)

## 2) Fill .env.common (shared settings)

Put only provider-agnostic settings here.

Example:

HYPERLIQUID_PRIVATE_KEY=0x_your_agent_private_key
HYPERLIQUID_VAULT_ADDRESS=0x_your_main_wallet
HYPERLIQUID_NETWORK=mainnet

ASSETS="BTC ETH SOL OIL GOLD SILVER SPX"
INTERVAL="5m"

ENABLE_AI_TRADING=true
AI_CAPITAL_PCT=100

ENABLE_ALGO_TRADING=false
ALGO_CAPITAL_PCT=0
ALGO_FILE_PATH=algo.py

MAX_POSITION_PCT=20
MAX_LOSS_PER_POSITION_PCT=20
MAX_LEVERAGE=10
MAX_TOTAL_EXPOSURE_PCT=80
DAILY_LOSS_CIRCUIT_BREAKER_PCT=25
MANDATORY_SL_PCT=5
MAX_CONCURRENT_POSITIONS=10
MIN_BALANCE_RESERVE_PCT=10
MIN_TRADE_CONFIDENCE=0.55

MAX_TOKENS=4096
ENABLE_TOOL_CALLING=false

## 3) Fill provider profile files

### .env.anthropic

AI_PROVIDER=anthropic
AI_MODEL=claude-sonnet-4-20250514
ANTHROPIC_API_KEY=your_anthropic_api_key_here

Optional:
# AI_SANITIZE_MODEL=claude-haiku-4-5-20251001
# THINKING_ENABLED=false
# THINKING_BUDGET_TOKENS=10000

### .env.openai

AI_PROVIDER=openai
AI_MODEL=gpt-4.1
OPENAI_API_KEY=your_openai_api_key_here

Optional:
# OPENAI_BASE_URL=https://api.openai.com/v1
# AI_SANITIZE_MODEL=gpt-4.1-mini

### .env.gemini

AI_PROVIDER=gemini
AI_MODEL=gemini-2.5-pro
GEMINI_API_KEY=your_gemini_api_key_here

Optional:
# AI_SANITIZE_MODEL=gemini-2.5-flash

## 4) Build runtime .env from common + provider profile

Run from project root.

Use Anthropic:
cat .env.common .env.anthropic > .env

Use OpenAI:
cat .env.common .env.openai > .env

Use Gemini:
cat .env.common .env.gemini > .env

## 5) Run hybrid mode (AI + algo together)

In .env.common set:
ENABLE_AI_TRADING=true
ENABLE_ALGO_TRADING=true
AI_CAPITAL_PCT=60
ALGO_CAPITAL_PCT=40

Rules:
- each enabled capital percent must be between 0 and 100
- enabled totals must not exceed 100

## 6) Quick verification

After building .env, verify active provider:

grep '^AI_PROVIDER=' .env
grep '^AI_MODEL=' .env

## 7) Optional helper aliases (zsh)

Add to your shell profile:

alias use_anthropic='cat .env.common .env.anthropic > .env && echo Active profile: anthropic'
alias use_openai='cat .env.common .env.openai > .env && echo Active profile: openai'
alias use_gemini='cat .env.common .env.gemini > .env && echo Active profile: gemini'

Then switch quickly:
- use_anthropic
- use_openai
- use_gemini

## 8) Security notes

- Never commit real API keys.
- Keep .env and profile files out of git if they contain secrets.
- Rotate keys immediately if exposed.
