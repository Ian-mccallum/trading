"""Feature computation and market-regime classification.

Pure functions over ``BarData`` sequences — no I/O, no database, no clock
reads. Floats are fine here: features feed *advisory* models (regime labels,
allocator scores) and are never used for order sizing or money math, which
stay Decimal end to end elsewhere.

``market_context`` is the convenience used to stamp ``Decision.context`` so
that every decision row carries the feature vector and regime label it was
made under; training later reads exactly what execution saw.

Regime taxonomy (``classify_regime``):

- ``trend_up`` / ``trend_down``: |sma20/sma50 - 1| >= 1%, sign decides.
- ``high_vol_range`` / ``low_vol_range``: no trend; split on annualized
  realized volatility at 25%.
- ``unknown``: not enough history to compute the inputs.
"""

from __future__ import annotations

import math
from typing import Any

from app.schemas.core import BarData

TRADING_DAYS_PER_YEAR = 252
TREND_THRESHOLD = 0.01  # |sma_ratio_20_50| at/above this is a trend
HIGH_VOL_THRESHOLD = 0.25  # annualized realized vol split for range regimes

REGIMES = ("trend_up", "trend_down", "high_vol_range", "low_vol_range", "unknown")


def _sma(values: list[float], n: int) -> float | None:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def _realized_vol(closes: list[float], period: int = 20) -> float | None:
    """Annualized sample stdev of the last ``period`` log returns."""
    if len(closes) < period + 1:
        return None
    window = closes[-(period + 1):]
    if any(c <= 0 for c in window):
        return None
    rets = [math.log(window[i] / window[i - 1]) for i in range(1, len(window))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS_PER_YEAR)


def _wilder_rsi(closes: list[float], period: int = 14) -> float | None:
    """RSI with Wilder smoothing over the full available history."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0.0:
        # No losses: RSI is 100 by convention; a perfectly flat series is neutral.
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _atr_pct(bars: list[BarData], period: int = 14) -> float | None:
    """Wilder-smoothed ATR divided by the last close."""
    if len(bars) < period + 1:
        return None
    true_ranges: list[float] = []
    for prev, cur in zip(bars[:-1], bars[1:], strict=True):
        prev_close = float(prev.close)
        high, low = float(cur.high), float(cur.low)
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    last_close = float(bars[-1].close)
    if last_close <= 0:
        return None
    return atr / last_close


def compute_features(bars: list[BarData]) -> dict[str, float | None]:
    """Feature vector for a bar series (oldest → newest, current bar last).

    Returns ``{}`` with fewer than 2 bars. Individual features are ``None``
    when history is too short for them, so callers/JSON stay well-formed.
    """
    if len(bars) < 2:
        return {}
    closes = [float(b.close) for b in bars]

    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    sma_ratio: float | None = None
    if sma20 is not None and sma50 is not None and sma50 != 0.0:
        sma_ratio = sma20 / sma50 - 1.0

    return {
        "ret_1": closes[-1] / closes[-2] - 1.0 if closes[-2] != 0.0 else 0.0,
        "sma_ratio_20_50": sma_ratio,
        "realized_vol_20": _realized_vol(closes),
        "rsi_14": _wilder_rsi(closes),
        "atr_pct_14": _atr_pct(bars),
    }


def classify_regime(features: dict[str, Any]) -> str:
    """Map a feature dict to one of ``REGIMES``. Missing inputs → unknown."""
    sma_ratio = features.get("sma_ratio_20_50")
    vol = features.get("realized_vol_20")
    if sma_ratio is None or vol is None:
        return "unknown"
    if sma_ratio >= TREND_THRESHOLD:
        return "trend_up"
    if sma_ratio <= -TREND_THRESHOLD:
        return "trend_down"
    return "high_vol_range" if vol >= HIGH_VOL_THRESHOLD else "low_vol_range"


def market_context(bars: list[BarData]) -> dict[str, Any]:
    """Convenience payload stamped onto ``Decision.context``."""
    features = compute_features(bars)
    return {"features": features, "regime": classify_regime(features)}
