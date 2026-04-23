"""Application composition root helpers."""

from __future__ import annotations

import asyncio
import logging

from src.application.cycle_runner import CycleRunner
from src.application.decision_pipeline import DecisionPipeline
from src.application.execution_service import ExecutionService
from src.application.market_data_service import MarketDataService
from src.application.reconciliation_service import ReconciliationService
from src.config import Settings
from src.exchanges.hyperliquid_adapter import HyperliquidBroker
from src.interfaces.api_server import ApiServer
from src.risk_manager import RiskManager
from src.strategies.ai_strategy import AIStrategy
from src.strategies.algo_strategy import AlgoStrategy
from src.strategies.base import Strategy


class ApplicationRuntime:
    """Fully wired application runtime."""

    def __init__(self, settings: Settings, assets: list[str], interval: str):
        self.settings = settings
        self.assets = assets
        self.interval = interval
        self.shutdown_event = asyncio.Event()

        self.broker = HyperliquidBroker(settings)
        self.risk_manager = RiskManager(settings=settings)
        self.market_data_service = MarketDataService(self.broker)
        self.decision_pipeline = DecisionPipeline()
        self.reconciliation_service = ReconciliationService(self.broker, self.risk_manager)
        self.execution_service = ExecutionService(
            self.broker,
            self.risk_manager,
            self.reconciliation_service,
        )
        self.api_server = ApiServer(settings)

        strategies: list[tuple[Strategy, float]] = []
        if settings.execution.enable_ai_trading:
            strategies.append(
                (
                    AIStrategy(
                        settings=settings,
                        broker=self.broker,
                        prompt_builder=self.decision_pipeline,
                    ),
                    settings.execution.ai_capital_pct,
                )
            )
        if settings.execution.enable_algo_trading:
            strategies.append(
                (
                    AlgoStrategy(settings=settings),
                    settings.execution.algo_capital_pct,
                )
            )
        self.cycle_runner = CycleRunner(
            assets=assets,
            interval=interval,
            strategies=strategies,
            market_data_service=self.market_data_service,
            decision_pipeline=self.decision_pipeline,
            execution_service=self.execution_service,
            reconciliation_service=self.reconciliation_service,
            risk_manager=self.risk_manager,
            shutdown_event=self.shutdown_event,
        )

    def request_shutdown(self) -> None:
        logging.info("Shutdown signal received — will stop after current cycle completes")
        self.shutdown_event.set()
