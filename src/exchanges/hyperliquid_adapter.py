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

    async def validate_assets(self, assets: list[str]) -> None:
        """Fail fast when configured symbols are not listed in Hyperliquid metadata."""

        await self.preload_assets(assets)
        available = self._available_asset_names()
        invalid = [asset for asset in assets if asset not in available]
        if invalid:
            examples = ", ".join(sorted(list(available))[:20])
            raise RuntimeError(
                "Invalid ASSETS configuration; not found on Hyperliquid: "
                f"{', '.join(invalid)}. Example available symbols: {examples}"
            )

    def _available_asset_names(self) -> set[str]:
        available: set[str] = set()
        if self._meta_cache and isinstance(self._meta_cache, list):
            meta = self._meta_cache[0]
            for item in meta.get("universe", []):
                name = item.get("name")
                if name:
                    available.add(str(name))

        for dex, payload in self._hip3_meta_cache.items():
            if not isinstance(payload, list) or not payload:
                continue
            meta = payload[0]
            for item in meta.get("universe", []):
                name = item.get("name")
                if not name:
                    continue
                available.add(str(name))
                available.add(f"{dex}:{name}")
        return available
