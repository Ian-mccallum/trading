"""Indicator library tests — hand-computed values on tiny fixed series."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.schemas.core import BarData
from app.strategies.indicators import (
    atr,
    realized_vol,
    rolling_high,
    rolling_low,
    sma,
    trailing_return,
    vol_scale,
    wilder_rsi,
    zscore,
)
from tests.conftest import make_bars


def bars_from_ohlc(rows: list[tuple[float, float, float, float]]) -> list[BarData]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        BarData(
            symbol="X", timeframe="1Day", ts=start + timedelta(days=i),
            open=Decimal(str(o)), high=Decimal(str(h)),
            low=Decimal(str(low)), close=Decimal(str(c)),
        )
        for i, (o, h, low, c) in enumerate(rows)
    ]


def test_sma():
    assert sma([1, 2, 3, 4], 2) == 3.5
    assert sma([1, 2], 3) is None
    assert sma([1], 0) is None


def test_rolling_high_low_exclude_current_bar():
    bars = bars_from_ohlc([(1, 10, 1, 5), (1, 12, 2, 6), (1, 11, 3, 20)])
    # Window over the two bars BEFORE the current one — the Donchian convention.
    assert rolling_high(bars, 2) == 12.0
    assert rolling_low(bars, 2) == 1.0
    # Including the current bar would see its high of 11 < close 20 anyway,
    # but the exclusion is what makes a same-bar breakout detectable at all.
    assert rolling_high(bars, 3, exclude_last=False) == 12.0
    assert rolling_high(bars, 3) is None  # only 2 prior bars available


def test_wilder_rsi_bounds():
    rising = [float(i) for i in range(1, 30)]
    falling = [float(30 - i) for i in range(29)]
    assert wilder_rsi(rising, 14) == 100.0
    assert wilder_rsi(falling, 14) == 0.0
    assert wilder_rsi([1.0] * 20, 14) == 50.0
    assert wilder_rsi([1.0, 2.0], 14) is None


def test_atr_hand_computed():
    # Constant 2-point daily ranges, no gaps: ATR == 2 regardless of smoothing.
    bars = bars_from_ohlc([(5, 6, 4, 5)] * 10)
    assert atr(bars, 3) == 2.0
    assert atr(bars, 20) is None


def test_trailing_return_with_skip():
    values = [100.0] * 10 + [110.0, 120.0]  # len 12
    # lookback 11 (from values[0]=100), skip 1 (end at values[-2]=110).
    assert trailing_return(values, 11, skip=1) == 0.10000000000000009
    assert trailing_return(values, 11, skip=0) == 0.19999999999999996
    assert trailing_return(values, 3, skip=3) is None  # lookback must exceed skip
    assert trailing_return([100.0, 110.0], 5) is None


def test_zscore():
    assert zscore([1.0] * 10, 5) == 0.0
    z = zscore([10.0, 10.0, 10.0, 10.0, 20.0], 5)
    assert z is not None and z > 1.5  # latest value well above the mean
    assert zscore([1.0], 5) is None


def test_realized_vol_flat_is_zero():
    assert realized_vol([100.0] * 30, 20) == 0.0
    vol = realized_vol([100.0 * math.exp(0.01 * ((-1) ** i)) for i in range(30)], 20)
    assert vol is not None and vol > 0


def test_vol_scale_caps_at_one():
    closes = [float(b.close) for b in make_bars(n=40, start_price=100, step=0.0)]
    assert vol_scale(closes, 0.15, 20) == 1.0  # zero vol -> no scaling
    # A wildly volatile series scales DOWN, never up.
    wild = [100.0, 150.0, 90.0, 160.0, 80.0] * 10
    scale = vol_scale(wild, 0.15, 20)
    assert 0.0 < scale < 1.0
    assert vol_scale(wild, 0.0, 20) == 1.0  # disabled target -> neutral
