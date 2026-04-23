"""Application-facing Hyperliquid adapter."""

from __future__ import annotations

from src.config import Settings
from src.trading.hyperliquid_api import HyperliquidAPI


class HyperliquidBroker(HyperliquidAPI):
    """Thin adapter that exposes the existing Hyperliquid client through ports."""

    def __init__(self, settings: Settings):
        super().__init__(settings=settings)

    async def preload_assets(self, assets: list[str]) -> None:
        await self.get_meta_and_ctxs()
        loaded_dexes: set[str] = set()
        for asset in assets:
            if ":" not in asset:
                continue
            dex = asset.split(":")[0]
            if dex in loaded_dexes:
                continue
            await self.get_meta_and_ctxs(dex=dex)
            loaded_dexes.add(dex)
