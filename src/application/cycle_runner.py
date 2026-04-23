"""Trading cycle orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from src.application.decision_pipeline import DecisionPipeline
from src.application.execution_service import ExecutionService
from src.application.market_data_service import MarketDataService
from src.application.reconciliation_service import ReconciliationService
from src.domain.models import ActiveTradeRecord, DecisionContext, InvocationMetadata
from src.risk_manager import RiskManager
from src.strategies.base import Strategy
from src.utils.state_persistence import load_active_trades, save_active_trades


def get_interval_seconds(interval_str: str) -> int:
    if interval_str.endswith("m"):
        return int(interval_str[:-1]) * 60
    if interval_str.endswith("h"):
        return int(interval_str[:-1]) * 3600
    if interval_str.endswith("d"):
        return int(interval_str[:-1]) * 86400
    raise ValueError(f"Unsupported interval: {interval_str}")


class CycleRunner:
    """Coordinates market-data, strategy, risk, and execution services."""

    def __init__(
        self,
        assets: list[str],
        interval: str,
        strategies: list[tuple[Strategy, float]],
        market_data_service: MarketDataService,
        decision_pipeline: DecisionPipeline,
        execution_service: ExecutionService,
        reconciliation_service: ReconciliationService,
        risk_manager: RiskManager,
        shutdown_event: asyncio.Event,
        diary_path: str = "diary.jsonl",
        decisions_path: str = "decisions.jsonl",
    ):
        self.assets = assets
        self.interval = interval
        self.strategies = strategies
        self.market_data_service = market_data_service
        self.decision_pipeline = decision_pipeline
        self.execution_service = execution_service
        self.reconciliation_service = reconciliation_service
        self.risk_manager = risk_manager
        self.shutdown_event = shutdown_event
        self.diary_path = diary_path
        self.decisions_path = decisions_path

        self.start_time = datetime.now(timezone.utc)
        self.invocation_count = 0
        self.trade_log: list[dict] = []
        self.active_trades = [
            ActiveTradeRecord.from_dict(trade) for trade in load_active_trades()
        ]

    async def run(self) -> None:
        await self.market_data_service.broker.preload_assets(self.assets)
        interval_secs = get_interval_seconds(self.interval)

        logging.info("Loaded %d active trades from disk", len(self.active_trades))
        try:
            await self.reconciliation_service.bootstrap_active_trades(
                self.active_trades,
                tracked_assets=self.assets,
            )
        except Exception as exc:
            logging.error("Startup reconciliation failed: %s", exc)

        while not self.shutdown_event.is_set():
            cycle_start = datetime.now(timezone.utc)
            self.invocation_count += 1
            minutes_since_start = (cycle_start - self.start_time).total_seconds() / 60

            try:
                state, account_value = await self.market_data_service.fetch_account_state()
                await self.reconciliation_service.force_close_losers(
                    state, self.active_trades, cycle_start
                )
                state, account_value = await self.market_data_service.fetch_account_state()
            except Exception as exc:
                logging.error("Risk force-close error: %s", exc)
                state, account_value = await self.market_data_service.fetch_account_state()

            try:
                open_orders = await self.reconciliation_service.reconcile_active_trades(
                    state, self.active_trades, cycle_start, tracked_assets=self.assets
                )
            except Exception as exc:
                logging.error("Reconcile error: %s", exc)
                open_orders = []

            try:
                fills = await self.market_data_service.broker.get_recent_fills(limit=50)
            except Exception:
                fills = []

            dashboard = await self.market_data_service.build_dashboard(
                state=state,
                account_value=account_value,
                trade_log=self.trade_log,
                active_trades=self.active_trades,
                open_orders=open_orders,
                fills=fills,
            )

            market_snapshots, asset_prices = await self.market_data_service.build_market_snapshots(
                self.assets, cycle_start
            )

            invocation = InvocationMetadata(
                minutes_since_start=minutes_since_start,
                current_time=cycle_start,
                invocation_count=self.invocation_count,
                interval=self.interval,
            )

            strategy_contexts = []
            for strategy, capital_pct in self.strategies:
                capital_budget_usd = account_value * (capital_pct / 100.0)
                strategy_contexts.append(
                    (
                        strategy,
                        DecisionContext(
                            assets=self.assets,
                            market_snapshots=market_snapshots,
                            account_dashboard=dashboard,
                            risk_limits=self.risk_manager.get_risk_summary(),
                            invocation=invocation,
                            capital_budget_usd=capital_budget_usd,
                            provider_label=strategy.source,
                        ),
                    )
                )

            all_source_decisions, reasoning_chunks = await self.decision_pipeline.run_strategies(
                strategy_contexts
            )
            merged_decisions = self.decision_pipeline.merge_trade_decisions(
                all_source_decisions, self.assets
            )
            self._persist_cycle_log(
                cycle_start=cycle_start,
                reasoning_chunks=reasoning_chunks,
                merged_decisions=merged_decisions,
                account_value=account_value,
                balance=state["balance"],
            )

            await self.execution_service.execute(
                intents=merged_decisions,
                assets=self.assets,
                cycle_start=cycle_start,
                asset_prices=asset_prices,
                active_trades=self.active_trades,
                shutdown_event=self.shutdown_event,
                trade_log=self.trade_log,
            )

            elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
            sleep_for = max(0.0, interval_secs - elapsed)
            if sleep_for < interval_secs * 0.1:
                logging.warning(
                    "Cycle %d took %.1fs (%.0f%% of %ds interval) — barely any sleep remaining",
                    self.invocation_count,
                    elapsed,
                    elapsed / interval_secs * 100,
                    interval_secs,
                )
            if sleep_for > 0:
                try:
                    await asyncio.wait_for(self.shutdown_event.wait(), timeout=sleep_for)
                except asyncio.TimeoutError:
                    pass

        logging.info("Bot loop exited cleanly after %d cycles", self.invocation_count)
        save_active_trades([trade.to_dict() for trade in self.active_trades])

    def _persist_cycle_log(
        self,
        cycle_start: datetime,
        reasoning_chunks: list[str],
        merged_decisions: list,
        account_value: float,
        balance: float,
    ) -> None:
        cycle_log = {
            "timestamp": cycle_start.isoformat(),
            "cycle": self.invocation_count,
            "reasoning": "\n".join(reasoning_chunks)[:2000],
            "decisions": [
                {
                    "asset": decision.asset,
                    "action": decision.action,
                    "allocation_usd": decision.allocation_usd,
                    "rationale": decision.rationale,
                    "source": decision.source,
                    "confidence": decision.confidence,
                    "leverage": decision.leverage,
                }
                for decision in merged_decisions
            ],
            "account_value": round(account_value, 2),
            "balance": round(float(balance), 2),
        }
        try:
            with open(self.decisions_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(cycle_log) + "\n")
        except Exception:
            pass
