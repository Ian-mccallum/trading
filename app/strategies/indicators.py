"""Pure technical indicators for strategies.

Strategies may depend only on ``app.schemas`` and this module — never on
learning, risk, brokers, or the DB — so the indicator helpers they share live
here rather than in ``app.learning.features`` (which serves the advisory
layer and may evolve independently).

All functions are deterministic and side-effect free. Indicator *values* are
floats (they feed comparisons, not money math); anything that becomes an
order quantity is converted back to Decimal by the strategy itself.
"""

from __future__ import annotations

import math

from app.schemas.core import BarData

TRADING_DAYS_PER_YEAR = 252


def closes_of(bars: list[BarData]) -> list[float]:
    return [float(b.close) for b in bars]


def sma(values: list[float], window: int) -> float | None:
    """Simple moving average of the last ``window`` values."""
    if window <= 0 or len(values) < window:
        return None
    return sum(values[-window:]) / window


def rolling_high(bars: list[BarData], window: int, *, exclude_last: bool = True) -> float | None:
    """Highest high over the trailing window.

    ``exclude_last=True`` (the default) excludes the current bar — the classic
    Donchian convention. Comparing the current close against a window that
    already contains the current bar's high can never break out (lookahead
    pitfall).
    """
    pool = bars[:-1] if exclude_last else bars
    if window <= 0 or len(pool) < window:
        return None
    return max(float(b.high) for b in pool[-window:])


def rolling_low(bars: list[BarData], window: int, *, exclude_last: bool = True) -> float | None:
    pool = bars[:-1] if exclude_last else bars
    if window <= 0 or len(pool) < window:
        return None
    return min(float(b.low) for b in pool[-window:])


def wilder_rsi(values: list[float], period: int) -> float | None:
    """RSI with Wilder smoothing over the full available history."""
    if period < 1 or len(values) < period + 1:
        return None
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def atr(bars: list[BarData], period: int) -> float | None:
    """Wilder-smoothed Average True Range (the Turtles' "N")."""
    if period < 1 or len(bars) < period + 1:
        return None
    true_ranges: list[float] = []
    for prev, cur in zip(bars[:-1], bars[1:], strict=True):
        prev_close = float(prev.close)
        high, low = float(cur.high), float(cur.low)
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    value = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def realized_vol(values: list[float], window: int) -> float | None:
    """Annualized sample stdev of the last ``window`` daily log returns."""
    if window < 2 or len(values) < window + 1:
        return None
    tail = values[-(window + 1):]
    if any(v <= 0 for v in tail):
        return None
    rets = [math.log(tail[i] / tail[i - 1]) for i in range(1, len(tail))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS_PER_YEAR)


def trailing_return(values: list[float], lookback: int, skip: int = 0) -> float | None:
    """Return over ``[t-lookback, t-skip]`` — the momentum literature's
    convention of skipping the most recent period (e.g. 12-1 momentum:
    lookback=252, skip=21) to sidestep short-term reversal."""
    if lookback <= skip or len(values) < lookback + 1:
        return None
    start = values[-(lookback + 1)]
    end = values[-(skip + 1)] if skip > 0 else values[-1]
    if start <= 0:
        return None
    return end / start - 1.0


def zscore(values: list[float], window: int) -> float | None:
    """Z-score of the latest value against the trailing window (inclusive)."""
    if window < 2 or len(values) < window:
        return None
    tail = values[-window:]
    mean = sum(tail) / window
    var = sum((v - mean) ** 2 for v in tail) / (window - 1)
    std = math.sqrt(var)
    if std == 0.0:
        return 0.0
    return (values[-1] - mean) / std


def vol_scale(values: list[float], target_annual_vol: float, window: int) -> float:
    """Volatility-targeting scale factor in (0, 1].

    ``target / realized`` capped at 1.0 — on a no-leverage platform the
    overlay can only ever *shrink* a position (Barroso & Santa-Clara style
    risk management without borrowing). Falls back to 1.0 when history is too
    short or vol is degenerate.
    """
    if target_annual_vol <= 0:
        return 1.0
    vol = realized_vol(values, window)
    if vol is None or vol <= 0:
        return 1.0
    return min(1.0, target_annual_vol / vol)
