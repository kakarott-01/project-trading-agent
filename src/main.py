"""Application entrypoint and runtime composition."""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal

from src.app.bootstrap import ApplicationRuntime
from src.config import get_settings


_rot_handler = logging.handlers.RotatingFileHandler(
    "trading.log", maxBytes=10 * 1024 * 1024, backupCount=5
)
logging.basicConfig(
    handlers=[_rot_handler, logging.StreamHandler()],
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


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
    print(
        f"Modes: AI={settings.execution.enable_ai_trading} "
        f"({settings.execution.ai_capital_pct}%)  "
        f"Algo={settings.execution.enable_algo_trading} "
        f"({settings.execution.algo_capital_pct}%)"
    )
    if settings.execution.enable_ai_trading:
        print(f"Provider/model: {settings.ai.provider}/{settings.ai.model}")

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
