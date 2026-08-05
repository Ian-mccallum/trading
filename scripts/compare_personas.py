"""Backtest every registered strategy across the symbol set and rank them.

This is the point of the whole persona exercise: real history, identical
conditions, one table. Results persist as PerformanceReports and Experiments
so the learning layer and the dashboard can read them.

Two things it does that a naive comparison would not:

1. **Sizes every strategy from the owner profile**, so results are comparable.
   Comparing a strategy holding one $740 share against one holding $5,000 of
   the same symbol measures position size, not skill.
2. **Reports buy-and-hold for the same symbol and window** as the reference
   line. A strategy that trails simply holding the asset has not earned its
   complexity, and that context belongs next to every number.

Usage:
  .venv/bin/python -m scripts.compare_personas
  .venv/bin/python -m scripts.compare_personas --symbols SPY QQQ --timeframe 1Day
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.backtest.engine import BacktestConfig, BacktestEngine, save_backtest_result
from app.config import get_settings
from app.db.models import MarketBar, Strategy
from app.logging import configure_logging, get_logger
from app.profile.model import load_profile
from app.schemas.core import BarData
from app.strategies import registry

log = get_logger("scripts.compare")

#: Strategies whose quantity is portfolio-derived rather than a plain size.
#: They are backtested but flagged, because a single sleeve in isolation is a
#: fraction of a portfolio and its return is not comparable to a full position.
SELF_SIZED_NOTE = "portfolio leg"


async def load_bars(session, symbol: str, timeframe: str) -> list[BarData]:
    rows = (
        await session.execute(
            sa.select(MarketBar)
            .where(MarketBar.symbol == symbol, MarketBar.timeframe == timeframe)
            .order_by(MarketBar.ts.asc())
        )
    ).scalars().all()
    return [
        BarData(
            symbol=r.symbol, timeframe=r.timeframe, ts=r.ts, open=r.open,
            high=r.high, low=r.low, close=r.close, volume=r.volume,
        )
        for r in rows
    ]


def buy_and_hold(bars: list[BarData], capital: Decimal) -> float:
    """Reference return: buy at the first open, hold to the last close."""
    if len(bars) < 2:
        return 0.0
    entry, exit_price = float(bars[0].open), float(bars[-1].close)
    if entry <= 0:
        return 0.0
    shares = int(float(capital) // entry)
    if shares <= 0:
        return 0.0
    return shares * (exit_price - entry) / float(BacktestConfig().initial_cash)


def sized_params(row: Strategy, price: float, per_position: Decimal) -> dict:
    """Override the strategy's `qty` so every entrant risks similar capital."""
    params = dict(row.params or {})
    if "qty" not in params or price <= 0:
        return params
    qty = max(1, int(float(per_position) // price))
    params["qty"] = qty
    return params


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="*", default=None)
    parser.add_argument("--timeframe", default="1Day")
    parser.add_argument("--save", action="store_true", help="persist reports to the database")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(debug=False)
    registry.load_builtins()
    profile = load_profile()

    symbols = [s.upper() for s in (args.symbols or profile.symbols or ["SPY"])]
    per_position = profile.per_position_target()

    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    rows_by_symbol: dict[str, list] = {}
    async with factory() as session:
        strategies = (
            await session.execute(sa.select(Strategy).order_by(Strategy.name))
        ).scalars().all()
        bars_by_symbol = {s: await load_bars(session, s, args.timeframe) for s in symbols}

        print(f"\nProfile {profile.name!r}: ${per_position:,.0f} per position, "
              f"{profile.risk_appetite} appetite")
        print(f"Timeframe {args.timeframe}")
        print("Returns are on the backtest's full $100,000 capital, so every "
              "row (including the reference) reflects the same position size.\n")

        for symbol in symbols:
            bars = bars_by_symbol.get(symbol) or []
            if len(bars) < 60:
                print(f"{symbol}: only {len(bars)} bars stored — run scripts.backfill_bars")
                continue

            window = f"{bars[0].ts.date()} → {bars[-1].ts.date()}"
            bh = buy_and_hold(bars, per_position)
            print(f"═══ {symbol}  ({len(bars)} bars, {window}) ═══")
            print(f"{'strategy':30} {'return':>9} {'vs B&H':>9} {'maxDD':>8} "
                  f"{'sharpe':>7} {'trades':>7}")
            # Returns are on the backtest's TOTAL capital, not on the amount
            # deployed, so the reference must be computed the same way or the
            # comparison silently flatters every strategy.
            print(f"{'buy & hold (same size)':30} {bh * 100:+8.2f}% {'—':>9} "
                  f"{'—':>8} {'—':>7} {'—':>7}")

            results = []
            for row in strategies:
                try:
                    cls = registry.get_strategy_class(row.class_path)
                except ValueError:
                    continue
                params = sized_params(row, float(bars[-1].close), per_position)
                try:
                    strategy = cls(params=params)
                except ValueError as exc:
                    log.warning("strategy_params_invalid", strategy=row.name, error=str(exc))
                    continue
                if len(bars) < strategy.warmup_bars():
                    continue
                # A sleeve bound to another symbol produces nothing here; skip
                # rather than reporting a misleading flat line.
                bound = (row.params or {}).get("symbol")
                if bound and bound.upper() != symbol:
                    continue
                result = await BacktestEngine(BacktestConfig()).run(strategy, bars)
                metrics = result.metrics
                if metrics["num_trades"] == 0:
                    continue
                note = SELF_SIZED_NOTE if getattr(cls, "self_sized", False) else ""
                results.append((row, metrics, note))
                if args.save:
                    await save_backtest_result(
                        session, row, result, bars[0].ts, bars[-1].ts
                    )

            results.sort(key=lambda item: item[1]["total_return"], reverse=True)
            for row, m, note in results:
                delta = m["total_return"] - bh
                print(
                    f"{row.name[:30]:30} {m['total_return'] * 100:+8.2f}% "
                    f"{delta * 100:+8.2f}% {m['max_drawdown'] * 100:7.2f}% "
                    f"{m['sharpe']:7.2f} {m['num_trades']:7} {note}"
                )
            rows_by_symbol[symbol] = results
            print()

        if args.save:
            await session.commit()
            print("Saved performance reports and experiments to the database.\n")

    await engine.dispose()

    if rows_by_symbol:
        print("═══ Across all symbols ═══")
        tally: dict[str, list[float]] = {}
        for results in rows_by_symbol.values():
            for row, m, _ in results:
                tally.setdefault(row.name, []).append(m["total_return"])
        ranked = sorted(
            ((name, sum(v) / len(v), len(v)) for name, v in tally.items()),
            key=lambda item: item[1],
            reverse=True,
        )
        print(f"{'strategy':30} {'mean return':>12} {'symbols':>8}")
        for name, mean, count in ranked:
            print(f"{name[:30]:30} {mean * 100:+11.2f}% {count:8}")
        print(
            "\nMean return across symbols is a ranking aid, not evidence: a "
            "two-year window is far too short to separate skill from luck."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
