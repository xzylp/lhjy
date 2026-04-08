"""行情数据 API"""

from __future__ import annotations

from fastapi import APIRouter

from ..contracts import BarSnapshot, QuoteSnapshot
from ..infra.market_adapter import MarketDataAdapter


def build_router(adapter: MarketDataAdapter, mode: str = "mock") -> APIRouter:
    router = APIRouter(prefix="/market", tags=["market"])

    @router.get("/health")
    async def health():
        return {"status": "ok", "mode": mode}

    @router.get("/universe")
    async def get_universe(scope: str = "main-board"):
        if scope == "a-share":
            return {"symbols": adapter.get_a_share_universe()}
        return {"symbols": adapter.get_main_board_universe()}

    @router.get("/snapshots", response_model=list[QuoteSnapshot])
    async def get_snapshots(symbols: str = ""):
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else []
        return adapter.get_snapshots(symbol_list)

    @router.get("/bars", response_model=list[BarSnapshot])
    async def get_bars(symbols: str = "", period: str = "1d"):
        symbol_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else []
        return adapter.get_bars(symbol_list, period)

    @router.get("/sectors")
    async def get_sectors():
        return {"sectors": adapter.get_sectors()}

    @router.get("/sectors/{sector_name}/symbols")
    async def get_sector_symbols(sector_name: str):
        return {"symbols": adapter.get_sector_symbols(sector_name)}

    return router
