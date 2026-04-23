"""Validated settings and environment loading for the trading application."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

from src.utils.security import _SensitiveDict

load_dotenv()


def _get_env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if required and (value is None or value == ""):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _get_env_alias(
    names: list[str],
    default: str | None = None,
    required: bool = False,
) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value != "":
            return value
    if required:
        raise RuntimeError(
            f"Missing required environment variable (any of): {', '.join(names)}"
        )
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


def _get_list(name: str, default: list[str] | None = None) -> list[str] | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON list for {name}: {raw}") from exc
        if not isinstance(parsed, list):
            raise RuntimeError(f"Environment variable {name} must be a JSON list")
        return [str(item).strip().strip('"\'') for item in parsed if str(item).strip()]
    values = [item.strip().strip('"\'') for item in raw.replace(",", " ").split()]
    values = [value for value in values if value]
    return values or default


def _require_range(label: str, value: float, low: float, high: float) -> None:
    if value < low or value > high:
        raise RuntimeError(f"{label} must be between {low} and {high}")


@dataclass(frozen=True)
class HyperliquidSettings:
    private_key: str | None
    mnemonic: str | None
    base_url: str | None
    network: str
    vault_address: str | None


@dataclass(frozen=True)
class AISettings:
    provider: str
    model: str
    sanitize_model: str
    anthropic_api_key: str | None
    openai_api_key: str | None
    gemini_api_key: str | None
    openai_base_url: str | None
    max_tokens: int
    enable_tool_calling: bool
    thinking_enabled: bool
    thinking_budget_tokens: int


@dataclass(frozen=True)
class ExecutionModeSettings:
    enable_ai_trading: bool
    enable_algo_trading: bool
    ai_capital_pct: float
    algo_capital_pct: float
    algo_file_path: str


@dataclass(frozen=True)
class RuntimeSettings:
    assets: list[str]
    interval: str | None


@dataclass(frozen=True)
class RiskSettings:
    max_position_pct: float
    max_loss_per_position_pct: float
    max_leverage: float
    max_total_exposure_pct: float
    daily_loss_circuit_breaker_pct: float
    mandatory_sl_pct: float
    max_concurrent_positions: int
    min_balance_reserve_pct: float
    min_trade_confidence: float


@dataclass(frozen=True)
class ApiSettings:
    host: str
    port: int
    secret: str


@dataclass(frozen=True)
class LegacySettings:
    taapi_api_key: str | None
    openrouter_api_key: str | None


@dataclass(frozen=True)
class Settings:
    hyperliquid: HyperliquidSettings
    ai: AISettings
    execution: ExecutionModeSettings
    runtime: RuntimeSettings
    risk: RiskSettings
    api: ApiSettings
    legacy: LegacySettings

    @property
    def llm_provider(self) -> str:
        return self.ai.provider

    @property
    def llm_model(self) -> str:
        return self.ai.model

    def resolve_algo_path(
        self,
        cwd: Path | None = None,
        algo_file_path: str | None = None,
    ) -> Path:
        base_dir = (cwd or Path.cwd()).resolve()
        raw_path = Path(algo_file_path or self.execution.algo_file_path).expanduser()
        resolved = raw_path.resolve() if raw_path.is_absolute() else (base_dir / raw_path).resolve()
        allowed_roots = {base_dir, (base_dir / "user_strategies").resolve()}
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            raise RuntimeError(
                "ALGO_FILE_PATH must stay inside the repository root or user_strategies/"
            )
        if resolved.suffix != ".py":
            raise RuntimeError("ALGO_FILE_PATH must point to a Python file")
        return resolved

    def as_legacy_config(self) -> _SensitiveDict:
        return _SensitiveDict(
            {
                "hyperliquid_private_key": self.hyperliquid.private_key,
                "mnemonic": self.hyperliquid.mnemonic,
                "hyperliquid_base_url": self.hyperliquid.base_url,
                "hyperliquid_network": self.hyperliquid.network,
                "hyperliquid_vault_address": self.hyperliquid.vault_address,
                "enable_ai_trading": self.execution.enable_ai_trading,
                "enable_claude_trading": self.execution.enable_ai_trading,
                "enable_algo_trading": self.execution.enable_algo_trading,
                "ai_capital_pct": self.execution.ai_capital_pct,
                "claude_capital_pct": self.execution.ai_capital_pct,
                "algo_capital_pct": self.execution.algo_capital_pct,
                "algo_file_path": self.execution.algo_file_path,
                "llm_provider": self.ai.provider,
                "llm_model": self.ai.model,
                "sanitize_model": self.ai.sanitize_model,
                "anthropic_api_key": self.ai.anthropic_api_key,
                "openai_api_key": self.ai.openai_api_key,
                "gemini_api_key": self.ai.gemini_api_key,
                "openai_base_url": self.ai.openai_base_url,
                "max_tokens": self.ai.max_tokens,
                "enable_tool_calling": self.ai.enable_tool_calling,
                "thinking_enabled": self.ai.thinking_enabled,
                "thinking_budget_tokens": self.ai.thinking_budget_tokens,
                "assets": " ".join(self.runtime.assets),
                "interval": self.runtime.interval,
                "max_position_pct": self.risk.max_position_pct,
                "max_loss_per_position_pct": self.risk.max_loss_per_position_pct,
                "max_leverage": self.risk.max_leverage,
                "max_total_exposure_pct": self.risk.max_total_exposure_pct,
                "daily_loss_circuit_breaker_pct": self.risk.daily_loss_circuit_breaker_pct,
                "mandatory_sl_pct": self.risk.mandatory_sl_pct,
                "max_concurrent_positions": self.risk.max_concurrent_positions,
                "min_balance_reserve_pct": self.risk.min_balance_reserve_pct,
                "min_trade_confidence": self.risk.min_trade_confidence,
                "api_host": self.api.host,
                "api_port": self.api.port,
                "api_secret": self.api.secret,
                "taapi_api_key": self.legacy.taapi_api_key,
                "openrouter_api_key": self.legacy.openrouter_api_key,
            }
        )


def _load_settings() -> Settings:
    enable_ai_trading = _get_bool(
        "ENABLE_AI_TRADING", _get_bool("ENABLE_CLAUDE_TRADING", True)
    )
    provider = (
        _get_env_alias(["AI_PROVIDER", "LLM_PROVIDER"], "anthropic") or "anthropic"
    ).strip().lower()
    model = _get_env_alias(["AI_MODEL", "LLM_MODEL"], "claude-sonnet-4-20250514") or ""

    if provider == "anthropic":
        sanitize_default = "claude-haiku-4-5-20251001"
    elif provider == "openai":
        sanitize_default = "gpt-4.1-mini"
    elif provider == "gemini":
        sanitize_default = "gemini-2.5-flash"
    else:
        raise RuntimeError(
            f"Unsupported AI_PROVIDER '{provider}'. Expected one of: anthropic, openai, gemini"
        )

    execution = ExecutionModeSettings(
        enable_ai_trading=enable_ai_trading,
        enable_algo_trading=_get_bool("ENABLE_ALGO_TRADING", False),
        ai_capital_pct=float(
            _get_float("AI_CAPITAL_PCT", _get_float("CLAUDE_CAPITAL_PCT", 100.0)) or 0.0
        ),
        algo_capital_pct=float(_get_float("ALGO_CAPITAL_PCT", 0.0) or 0.0),
        algo_file_path=_get_env("ALGO_FILE_PATH", "algo.py") or "algo.py",
    )

    _require_range("AI_CAPITAL_PCT", execution.ai_capital_pct, 0, 100)
    _require_range("ALGO_CAPITAL_PCT", execution.algo_capital_pct, 0, 100)
    if not execution.enable_ai_trading and not execution.enable_algo_trading:
        raise RuntimeError("At least one execution mode must be enabled")
    total_enabled_pct = (
        (execution.ai_capital_pct if execution.enable_ai_trading else 0.0)
        + (execution.algo_capital_pct if execution.enable_algo_trading else 0.0)
    )
    if total_enabled_pct > 100:
        raise RuntimeError(
            f"Enabled capital allocation exceeds 100% (total={total_enabled_pct:.2f}%)"
        )

    ai = AISettings(
        provider=provider,
        model=model,
        sanitize_model=_get_env_alias(["AI_SANITIZE_MODEL", "SANITIZE_MODEL"], sanitize_default)
        or sanitize_default,
        anthropic_api_key=_get_env(
            "ANTHROPIC_API_KEY",
            required=execution.enable_ai_trading and provider == "anthropic",
        ),
        openai_api_key=_get_env(
            "OPENAI_API_KEY",
            required=execution.enable_ai_trading and provider == "openai",
        ),
        gemini_api_key=_get_env(
            "GEMINI_API_KEY",
            required=execution.enable_ai_trading and provider == "gemini",
        ),
        openai_base_url=_get_env("OPENAI_BASE_URL"),
        max_tokens=int(_get_int("MAX_TOKENS", 4096) or 4096),
        enable_tool_calling=_get_bool("ENABLE_TOOL_CALLING", False),
        thinking_enabled=_get_bool("THINKING_ENABLED", False),
        thinking_budget_tokens=int(_get_int("THINKING_BUDGET_TOKENS", 10000) or 10000),
    )

    assets = _get_list("ASSETS", []) or []
    runtime = RuntimeSettings(
        assets=assets,
        interval=_get_env("INTERVAL"),
    )

    risk = RiskSettings(
        max_position_pct=float(_get_float("MAX_POSITION_PCT", 20.0) or 20.0),
        max_loss_per_position_pct=float(
            _get_float("MAX_LOSS_PER_POSITION_PCT", 20.0) or 20.0
        ),
        max_leverage=float(_get_float("MAX_LEVERAGE", 10.0) or 10.0),
        max_total_exposure_pct=float(
            _get_float("MAX_TOTAL_EXPOSURE_PCT", 80.0) or 80.0
        ),
        daily_loss_circuit_breaker_pct=float(
            _get_float("DAILY_LOSS_CIRCUIT_BREAKER_PCT", 25.0) or 25.0
        ),
        mandatory_sl_pct=float(_get_float("MANDATORY_SL_PCT", 5.0) or 5.0),
        max_concurrent_positions=int(_get_int("MAX_CONCURRENT_POSITIONS", 10) or 10),
        min_balance_reserve_pct=float(
            _get_float("MIN_BALANCE_RESERVE_PCT", 10.0) or 10.0
        ),
        min_trade_confidence=float(_get_float("MIN_TRADE_CONFIDENCE", 0.55) or 0.55),
    )

    for label, value in (
        ("MAX_POSITION_PCT", risk.max_position_pct),
        ("MAX_LOSS_PER_POSITION_PCT", risk.max_loss_per_position_pct),
        ("MAX_TOTAL_EXPOSURE_PCT", risk.max_total_exposure_pct),
        ("DAILY_LOSS_CIRCUIT_BREAKER_PCT", risk.daily_loss_circuit_breaker_pct),
        ("MANDATORY_SL_PCT", risk.mandatory_sl_pct),
        ("MIN_BALANCE_RESERVE_PCT", risk.min_balance_reserve_pct),
    ):
        _require_range(label, value, 0, 100)
    if risk.max_leverage < 1:
        raise RuntimeError("MAX_LEVERAGE must be >= 1")
    if risk.max_concurrent_positions < 1:
        raise RuntimeError("MAX_CONCURRENT_POSITIONS must be >= 1")
    _require_range("MIN_TRADE_CONFIDENCE", risk.min_trade_confidence, 0, 1)

    api_port = int(_get_int("APP_PORT", _get_int("API_PORT", 3000)) or 3000)
    if api_port < 1 or api_port > 65535:
        raise RuntimeError("API_PORT must be between 1 and 65535")

    settings = Settings(
        hyperliquid=HyperliquidSettings(
            private_key=_get_env("HYPERLIQUID_PRIVATE_KEY")
            or _get_env("LIGHTER_PRIVATE_KEY"),
            mnemonic=_get_env("MNEMONIC"),
            base_url=_get_env("HYPERLIQUID_BASE_URL"),
            network=(_get_env("HYPERLIQUID_NETWORK", "mainnet") or "mainnet").lower(),
            vault_address=_get_env("HYPERLIQUID_VAULT_ADDRESS"),
        ),
        ai=ai,
        execution=execution,
        runtime=runtime,
        risk=risk,
        api=ApiSettings(
            host=_get_env("API_HOST", "127.0.0.1") or "127.0.0.1",
            port=api_port,
            secret=_get_env("API_SECRET", "") or "",
        ),
        legacy=LegacySettings(
            taapi_api_key=_get_env("TAAPI_API_KEY"),
            openrouter_api_key=_get_env("OPENROUTER_API_KEY"),
        ),
    )

    if not settings.hyperliquid.private_key and not settings.hyperliquid.mnemonic:
        raise RuntimeError(
            "Either HYPERLIQUID_PRIVATE_KEY/LIGHTER_PRIVATE_KEY or MNEMONIC must be provided"
        )

    settings.resolve_algo_path()
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache validated settings for the current process."""

    return _load_settings()
