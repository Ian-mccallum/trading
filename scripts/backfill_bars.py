"""Backfill historical bars from Alpaca market data into market_bars.

Usage: .venv/bin/python -m scripts.backfill_bars SPY QQQ --days 365 --timeframe 1Day
Requires ALPACA_PAPER_API_KEY/SECRET (data API works with paper keys).
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.base import utcnow
from app.logging import configure_logging, get_logger
from app.marketdata.alpaca_data import AlpacaDataProvider, store_bars

log = get_logger("scripts.backfill")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("symbols", nargs="+")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--timeframe", default="1Day")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(debug=True)
    if not settings.alpaca_paper_api_key:
        raise SystemExit("ALPACA_PAPER_API_KEY not set; cannot fetch market data")

    provider = AlpacaDataProvider(
        settings.alpaca_paper_api_key, settings.alpaca_paper_api_secret
    )
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    end = utcnow()
    start = end - timedelta(days=args.days)
    async with factory() as session:
        for symbol in args.symbols:
            bars = await provider.get_bars(symbol.upper(), args.timeframe, start, end)
            n = await store_bars(session, bars)
            await session.commit()
            log.info("backfilled", symbol=symbol.upper(), fetched=len(bars), stored=n)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
