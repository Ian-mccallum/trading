"""Benchmark series support for relative-strength criteria.

Several persona strategies need to answer "is this symbol outperforming the
market?" — Minervini's criterion 8 (RS ranking), O'Neil's *L* (leader vs
laggard), Dual Momentum's relative leg. A single-symbol strategy cannot see
another symbol's bars, so the worker computes a compact benchmark summary and
injects it into ``StrategyContext.features["benchmark"]``.

Design rules:

- Strategies stay pure: they read the summary from the context they are
  handed, never fetch anything.
- The summary is **optional**. Every consumer must degrade gracefully when it
  is missing (short history, benchmark bars not yet backfilled) and record
  that it did so, rather than silently changing meaning.
- ``relative_return`` is a *benchmark-relative* measure, NOT an IBD-style
  1–99 universe percentile. It captures the intent (outperformance) but is a
  documented approximation — see docs/investor-personas-research.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.schemas.core import BarData
from app.strategies.indicators import closes_of, sma, trailing_return

FEATURE_KEY = "benchmark"
PEERS_KEY = "peers"

#: Lookbacks summarized for consumers, in trading days.
LOOKBACKS = {"return_1m": 21, "return_3m": 63, "return_6m": 126, "return_12m": 252}

#: Window for the benchmark's own trend flag. O'Neil's *M* (market direction)
#: is "only buy in a confirmed market uptrend"; this is the mechanical proxy.
TREND_WINDOW = 200


@dataclass(frozen=True)
class BenchmarkView:
    """Read-only view over the injected benchmark summary."""

    symbol: str
    returns: dict[str, float]
    #: Whether the benchmark itself is above its long-term average. None when
    #: history was too short — consumers must skip the check, not assume.
    above_trend: bool | None = None

    def get_return(self, key: str) -> float | None:
        return self.returns.get(key)


def _summarize(symbol: str, bars: list[BarData]) -> dict[str, Any]:
    if len(bars) < 2:
        return {}
    closes = closes_of(bars)
    returns = {
        name: value
        for name, window in LOOKBACKS.items()
        if (value := trailing_return(closes, window)) is not None
    }
    if not returns:
        return {}
    summary: dict[str, Any] = {"symbol": symbol, "returns": returns}
    trend = sma(closes, TREND_WINDOW)
    if trend is not None:
        summary["above_trend"] = closes[-1] > trend
    return summary


def build_benchmark_features(symbol: str, bars: list[BarData]) -> dict[str, Any]:
    """Compact, JSON-safe summary of a benchmark's trailing performance.

    Returns ``{}`` when there is not enough history to compute any lookback,
    so callers can simply skip injecting the key.
    """
    return _summarize(symbol, bars)


def build_peer_features(series: dict[str, list[BarData]]) -> dict[str, Any]:
    """Summaries for several symbols at once, keyed by symbol.

    Dual momentum's relative leg is inherently a comparison between two
    instruments, which a single-symbol strategy cannot perform alone. The
    worker already loads every allowlisted symbol's bars, so summarizing them
    into the context is nearly free and keeps strategies pure.
    """
    peers = {}
    for symbol, bars in series.items():
        summary = _summarize(symbol, bars)
        if summary:
            peers[symbol.upper()] = summary
    return peers


def read_peer(features: dict[str, Any] | None, symbol: str) -> BenchmarkView | None:
    """One peer's summary from the injected map, or None if unavailable."""
    if not features:
        return None
    peers = features.get(PEERS_KEY)
    if not isinstance(peers, dict):
        return None
    return _view(peers.get(symbol.upper()))


def _view(raw: Any) -> BenchmarkView | None:
    """Validate one summary dict into a view, or None.

    Returning None for anything half-valid is deliberate: consumers skip the
    criterion on None, and a partially-parsed view would silently change what
    a screen means.
    """
    if not isinstance(raw, dict):
        return None
    symbol = raw.get("symbol")
    returns = raw.get("returns")
    if not isinstance(symbol, str) or not isinstance(returns, dict):
        return None
    clean = {
        k: float(v)
        for k, v in returns.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    if not clean:
        return None
    above = raw.get("above_trend")
    return BenchmarkView(
        symbol=symbol,
        returns=clean,
        above_trend=above if isinstance(above, bool) else None,
    )


def read_benchmark(features: dict[str, Any] | None) -> BenchmarkView | None:
    """Safely extract the benchmark view from strategy features.

    Returns None when absent or malformed — consumers must handle None by
    skipping the relative-strength criterion, never by assuming a value.
    """
    if not features:
        return None
    return _view(features.get(FEATURE_KEY))


def relative_return(
    bars: list[BarData], benchmark: BenchmarkView | None, lookback_key: str = "return_12m"
) -> float | None:
    """Symbol's trailing return minus the benchmark's over the same window.

    Positive means outperformance. Returns None when either side is
    unavailable — the caller must then skip the criterion.
    """
    if benchmark is None:
        return None
    bench = benchmark.get_return(lookback_key)
    window = LOOKBACKS.get(lookback_key)
    if bench is None or window is None:
        return None
    own = trailing_return(closes_of(bars), window)
    if own is None:
        return None
    return own - bench
