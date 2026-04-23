"""Strategy interfaces used by the application layer."""

from __future__ import annotations

from typing import Protocol

from src.domain.models import DecisionContext, StrategyResult


class Strategy(Protocol):
    source: str

    async def generate(self, context: DecisionContext) -> StrategyResult:
        """Return a normalized strategy result for the current cycle."""
