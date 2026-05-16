"""Application entrypoint and runtime composition."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import signal

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

from src.app.bootstrap import ApplicationRuntime
from src.config import get_settings
from src.utils.paths import data_path

load_dotenv(override=True)


class _JsonFormatter(logging.Formatter):
    """Minimal JSON formatter for machine-readable file logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_rot_handler = logging.handlers.RotatingFileHandler(
    data_path("trading.log"), maxBytes=10 * 1024 * 1024, backupCount=5
)
_rot_handler.setFormatter(_JsonFormatter())
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
)
logging.basicConfig(
    handlers=[_rot_handler, _stream_handler],
    level=logging.INFO,
)

RISK_ENV_VARS = {
    "MAX_POSITION_PCT",
    "MAX_LOSS_PER_POSITION_PCT",
    "MAX_LEVERAGE",
    "MAX_TOTAL_EXPOSURE_PCT",
    "MAX_CORRELATED_BASKET_EXPOSURE_PCT",
    "DAILY_LOSS_CIRCUIT_BREAKER_PCT",
    "MANDATORY_SL_PCT",
    "MAX_CONCURRENT_POSITIONS",
    "MIN_BALANCE_RESERVE_PCT",
    "MIN_TRADE_CONFIDENCE",
}


def clear_terminal() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AI-based Trading Agent on Hyperliquid")
    parser.add_argument("--assets", type=str, nargs="+", required=False)
    parser.add_argument("--interval", type=str, required=False)
    return parser.parse_args()


def resolve_runtime_targets(
    cli_assets: list[str] | None,
    cli_interval: str | None,
) -> tuple[list[str], str]:
    settings = get_settings()
    assets = list(cli_assets or settings.runtime.assets)
    interval = cli_interval or settings.runtime.interval
    if not assets or not interval:
        raise SystemExit(
            "Provide --assets and --interval, or set ASSETS and INTERVAL in .env"
        )
    return assets, interval


async def main_async() -> None:
    clear_terminal()
    args = parse_args()
    settings = get_settings()
    assets, interval = resolve_runtime_targets(args.assets, args.interval)
    runtime = ApplicationRuntime(settings=settings, assets=assets, interval=interval)

    print(f"Starting trading agent: assets={assets}  interval={interval}")
    if settings.runtime.dry_run:
        print(
            "DRY RUN ENABLED: using real market data and AI decisions, "
            "but simulating all fills locally."
        )
        logging.warning(
            "DRY RUN ENABLED: no live orders will be submitted; virtual balance starts at $%.2f",
            settings.runtime.dry_run_initial_balance,
        )
    print(
        f"Modes: AI={settings.execution.enable_ai_trading} "
        f"({settings.execution.ai_capital_pct}%)  "
        f"Algo={settings.execution.enable_algo_trading} "
        f"({settings.execution.algo_capital_pct}%)"
    )
    if settings.execution.enable_ai_trading:
        print(f"Provider/model: {settings.ai.provider}/{settings.ai.model}")

    configured_risk_vars = sorted(name for name in RISK_ENV_VARS if name in os.environ)
    if settings.risk.safe_retail_mode and configured_risk_vars:
        logging.warning(
            "SAFE_RETAIL_MODE=%s with preset=%s overrides configured risk env vars (%s). "
            "Effective caps: max_leverage=%.2fx, max_position_pct=%.2f%%, "
            "max_total_exposure_pct=%.2f%%, daily_loss_circuit_breaker_pct=%.2f%%.",
            settings.risk.safe_retail_mode,
            settings.risk.safe_retail_preset,
            ", ".join(configured_risk_vars),
            settings.risk.max_leverage,
            settings.risk.max_position_pct,
            settings.risk.max_total_exposure_pct,
            settings.risk.daily_loss_circuit_breaker_pct,
        )

    await runtime.api_server.start()
    logging.info("API listening on %s:%d", settings.api.host, settings.api.port)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, runtime.request_shutdown)
        except NotImplementedError:
            pass

    try:
        await runtime.cycle_runner.run()
    finally:
        await runtime.api_server.stop()


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
