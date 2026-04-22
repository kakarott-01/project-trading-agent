"""Centralized environment variable loading for the trading agent configuration."""

import json
import os
from dotenv import load_dotenv

load_dotenv()

# Import AFTER dotenv so the class is available immediately
from src.utils.security import _SensitiveDict  # noqa: E402


def _get_env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _get_env_alias(names: list[str], default: str | None = None, required: bool = False) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value != "":
            return value
    if required:
        raise RuntimeError(f"Missing required environment variable (any of): {', '.join(names)}")
    return default


def _get_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid integer for {name}: {raw}") from exc


def _get_float(name: str, default: float | None = None) -> float | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"Invalid float for {name}: {raw}") from exc


def _get_json(name: str, default: dict | None = None) -> dict | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise RuntimeError(f"Environment variable {name} must be a JSON object")
        return parsed
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON for {name}: {raw}") from exc


def _get_list(name: str, default: list[str] | None = None) -> list[str] | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                raise RuntimeError(f"Environment variable {name} must be a list if using JSON syntax")
            return [str(item).strip().strip('"\'') for item in parsed if str(item).strip()]
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON list for {name}: {raw}") from exc
    values = []
    for item in raw.split(","):
        cleaned = item.strip().strip('"\'')
        if cleaned:
            values.append(cleaned)
    return values or default


enable_ai_trading = _get_bool("ENABLE_AI_TRADING", _get_bool("ENABLE_CLAUDE_TRADING", True))
llm_provider = (_get_env_alias(["AI_PROVIDER", "LLM_PROVIDER"], "anthropic") or "anthropic").strip().lower()
llm_model = _get_env_alias(["AI_MODEL", "LLM_MODEL"], "claude-sonnet-4-20250514")

if llm_provider == "anthropic":
    sanitize_default = "claude-haiku-4-5-20251001"
elif llm_provider == "openai":
    sanitize_default = "gpt-4.1-mini"
elif llm_provider == "gemini":
    sanitize_default = "gemini-2.5-flash"
else:
    sanitize_default = llm_model or ""


# Use _SensitiveDict so private keys are never printed in tracebacks or logs
CONFIG = _SensitiveDict({
    # Hyperliquid
    "hyperliquid_private_key": _get_env("HYPERLIQUID_PRIVATE_KEY") or _get_env("LIGHTER_PRIVATE_KEY"),
    "mnemonic": _get_env("MNEMONIC"),
    "hyperliquid_base_url": _get_env("HYPERLIQUID_BASE_URL"),
    "hyperliquid_network": _get_env("HYPERLIQUID_NETWORK", "mainnet"),
    "hyperliquid_vault_address": _get_env("HYPERLIQUID_VAULT_ADDRESS"),

    # Execution mode
    "enable_ai_trading": enable_ai_trading,
    "enable_claude_trading": enable_ai_trading,
    "enable_algo_trading": _get_bool("ENABLE_ALGO_TRADING", False),
    "ai_capital_pct": _get_float("AI_CAPITAL_PCT", _get_float("CLAUDE_CAPITAL_PCT", 100.0)),
    "claude_capital_pct": _get_float("AI_CAPITAL_PCT", _get_float("CLAUDE_CAPITAL_PCT", 100.0)),
    "algo_capital_pct": _get_float("ALGO_CAPITAL_PCT", 0.0),
    "algo_file_path": _get_env("ALGO_FILE_PATH", "algo.py"),

    # LLM provider + model selection
    "llm_provider": llm_provider,
    "llm_model": llm_model,
    "sanitize_model": _get_env_alias(["AI_SANITIZE_MODEL", "SANITIZE_MODEL"], sanitize_default),

    # Provider credentials
    "anthropic_api_key": _get_env("ANTHROPIC_API_KEY", required=enable_ai_trading and llm_provider == "anthropic"),
    "openai_api_key": _get_env("OPENAI_API_KEY", required=enable_ai_trading and llm_provider == "openai"),
    "gemini_api_key": _get_env("GEMINI_API_KEY", required=enable_ai_trading and llm_provider == "gemini"),

    # Optional provider endpoints
    "openai_base_url": _get_env("OPENAI_BASE_URL"),

    "max_tokens": _get_int("MAX_TOKENS", 4096),
    "enable_tool_calling": _get_bool("ENABLE_TOOL_CALLING", False),

    # Extended thinking (Anthropic only)
    "thinking_enabled": _get_bool("THINKING_ENABLED", False),
    "thinking_budget_tokens": _get_int("THINKING_BUDGET_TOKENS", 10000),

    # Runtime controls
    "assets": _get_env("ASSETS"),
    "interval": _get_env("INTERVAL"),

    # Risk management
    "max_position_pct": _get_env("MAX_POSITION_PCT", "20"),
    "max_loss_per_position_pct": _get_env("MAX_LOSS_PER_POSITION_PCT", "20"),
    "max_leverage": _get_env("MAX_LEVERAGE", "10"),
    "max_total_exposure_pct": _get_env("MAX_TOTAL_EXPOSURE_PCT", "80"),
    "daily_loss_circuit_breaker_pct": _get_env("DAILY_LOSS_CIRCUIT_BREAKER_PCT", "25"),
    "mandatory_sl_pct": _get_env("MANDATORY_SL_PCT", "5"),
    "max_concurrent_positions": _get_env("MAX_CONCURRENT_POSITIONS", "10"),
    "min_balance_reserve_pct": _get_env("MIN_BALANCE_RESERVE_PCT", "10"),
    "min_trade_confidence": _get_float("MIN_TRADE_CONFIDENCE", 0.55),

    # API server
    "api_host": _get_env("API_HOST", "127.0.0.1"),   # Default to loopback, NOT 0.0.0.0
    "api_port": _get_env("APP_PORT") or _get_env("API_PORT") or "3000",
    "api_secret": _get_env("API_SECRET", ""),  # Empty = unauthenticated (dev only)

    # Legacy / optional
    "taapi_api_key": _get_env("TAAPI_API_KEY"),
    "openrouter_api_key": _get_env("OPENROUTER_API_KEY"),
})