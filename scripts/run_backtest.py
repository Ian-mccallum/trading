"""Run a backtest for a registered strategy over stored bars and persist the
result as a PerformanceReport + Experiment.

Usage:
  .venv/bin/python -m scripts.run_backtest sma_cross --symbol SPY --timeframe 1Day
"""

from __future__ import annotations

import argparse
import asyncio

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.backtest.engine import BacktestConfig, BacktestEngine, save_backtest_result
from app.config import get_settings
from app.db.models import MarketBar, Strategy
from app.logging import configure_logging, get_logger
from app.schemas.core import BarData
from app.strategies import registry

log = get_logger("scripts.backtest")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("strategy_name")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--timeframe", default="1Day")
    parser.add_argument("--version", type=int, default=None)
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(debug=True)
    registry.load_builtins()

    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        stmt = sa.select(Strategy).where(Strategy.name == args.strategy_name)
        if args.version:
            stmt = stmt.where(Strategy.version == args.version)
        row = (
            (await session.execute(stmt.order_by(Strategy.version.desc()).limit(1)))
            .scalars()
            .first()
        )
        if row is None:
            raise SystemExit(f"strategy {args.strategy_name!r} not found; run scripts.seed first")

        bar_rows = (
            (
                await session.execute(
                    sa.select(MarketBar)
                    .where(
                        MarketBar.symbol == args.symbol.upper(),
                        MarketBar.timeframe == args.timeframe,
                    )
                    .order_by(MarketBar.ts.asc())
                )
            )
            .scalars()
            .all()
        )
        if len(bar_rows) < 60:
            raise SystemExit(
                f"only {len(bar_rows)} bars stored for {args.symbol}; "
                "run scripts.backfill_bars first"
            )
        bars = [
            BarData(
                symbol=b.symbol, timeframe=b.timeframe, ts=b.ts,
                open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume,
            )
            for b in bar_rows
        ]

        strategy = registry.instantiate(row)
        result = await BacktestEngine(BacktestConfig()).run(strategy, bars)
        await save_backtest_result(session, row, result, bars[0].ts, bars[-1].ts)
        await session.commit()
        log.info(
            "backtest_complete",
            strategy=row.name,
            version=row.version,
            symbol=args.symbol.upper(),
            bars=len(bars),
            **{k: str(v) for k, v in result.metrics.items()},
        )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
