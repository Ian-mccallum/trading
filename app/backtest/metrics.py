"""Backtest performance metrics.

Pure functions over an equity curve (``list[tuple[datetime, Decimal]]``) and a
trade list (dicts with ``ts``/``symbol``/``side``/``qty``/``price``). Inputs
may carry ``Decimal`` values; all math happens on floats and all results are
floats, since these numbers feed JSON metrics blobs and ranking heuristics —
not accounting. Degenerate inputs (empty/constant curves, no trades) return
0.0 rather than dividing by zero.

Round trips: buys are paired with subsequent sells FIFO; each matched
(buy-lot, sell) portion is one round trip with pnl
``(sell_price - buy_price) * matched_qty``.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

EquityCurve = Sequence[tuple[Any, Any]]  # (timestamp, Decimal | float)
Trade = Mapping[str, Any]


def _values(equity_curve: EquityCurve) -> list[float]:
    return [float(value) for _, value in equity_curve]


def _period_returns(values: list[float]) -> list[float]:
    return [
        curr / prev - 1.0
        for prev, curr in zip(values, values[1:], strict=False)
        if prev != 0
    ]


def total_return(equity_curve: EquityCurve) -> float:
    """Fractional return from first to last equity point (0.21 == +21%)."""
    values = _values(equity_curve)
    if len(values) < 2 or values[0] == 0:
        return 0.0
    return values[-1] / values[0] - 1.0


def max_drawdown(equity_curve: EquityCurve) -> float:
    """Largest peak-to-trough decline as a positive fraction of the peak."""
    worst = 0.0
    peak = -math.inf
    for value in _values(equity_curve):
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def sharpe(equity_curve: EquityCurve, periods_per_year: int = 252) -> float:
    """Annualized Sharpe ratio of per-period returns (risk-free rate 0),
    using the sample standard deviation. 0.0 when undefined (fewer than two
    returns, or zero volatility)."""
    returns = _period_returns(_values(equity_curve))
    if len(returns) < 2:
        return 0.0
    stdev = statistics.stdev(returns)
    if stdev == 0:
        return 0.0
    return statistics.fmean(returns) / stdev * math.sqrt(periods_per_year)


def round_trip_pnls(trades: Sequence[Trade]) -> list[float]:
    """Pair buys with subsequent sells FIFO; one pnl per matched portion.
    Unmatched open buys (still-held shares) produce no round trip; sells with
    no prior buy lot are ignored."""
    lots: deque[tuple[float, float]] = deque()  # (qty, buy_price), oldest first
    pnls: list[float] = []
    for trade in trades:
        side = str(trade["side"]).lower()
        qty = float(trade["qty"])
        price = float(trade["price"])
        if side == "buy":
            lots.append((qty, price))
            continue
        remaining = qty
        while remaining > 0 and lots:
            lot_qty, lot_price = lots[0]
            matched = min(remaining, lot_qty)
            pnls.append((price - lot_price) * matched)
            remaining -= matched
            if matched == lot_qty:
                lots.popleft()
            else:
                lots[0] = (lot_qty - matched, lot_price)
    return pnls


def win_rate(round_trip_trades: Sequence[float]) -> float:
    """Fraction of round trips with pnl > 0; 0.0 when there are none."""
    if not round_trip_trades:
        return 0.0
    wins = sum(1 for pnl in round_trip_trades if pnl > 0)
    return wins / len(round_trip_trades)


def num_trades(trades: Sequence[Trade]) -> int:
    return len(trades)


def compute_metrics(equity_curve: EquityCurve, trades: Sequence[Trade]) -> dict[str, Any]:
    """Assemble the standard metrics dict (all JSON-serializable scalars)."""
    pnls = round_trip_pnls(trades)
    return {
        "final_equity": float(equity_curve[-1][1]) if equity_curve else 0.0,
        "total_return": total_return(equity_curve),
        "max_drawdown": max_drawdown(equity_curve),
        "sharpe": sharpe(equity_curve),
        "win_rate": win_rate(pnls),
        "num_trades": num_trades(trades),
    }
