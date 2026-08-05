"""Bollinger Band mean reversion — bands per John Bollinger ("Bollinger on
Bollinger Bands", 2001); the lower-band-tag-to-mean trade is the standard
practitioner construction.

Canonical conventions encoded:

- Bands: SMA(20) ± 2.0 × **population** standard deviation of the same 20
  closes (divide by N — Bollinger's own convention; sample stdev differs
  noticeably at N=20).
- BUY when close < lower band; CLOSE when close ≥ middle band (the SMA).
- 200-day SMA trend gate, default ON — Bollinger's rule that "tags of the
  bands are not signals in and of themselves": in a downtrend price walks
  the lower band and a naive tag-buyer averages into a crash. Set
  ``trend_sma`` to null to disable (at your peril).
- Optional minimum bandwidth floor: skip entries when (upper−lower)/middle
  is below ``min_bandwidth`` — a −2σ move in a dead-quiet market is noise.
"""

from __future__ import annotations

import math
from decimal import Decimal

from app.db.models import SignalAction
from app.logging import get_logger
from app.schemas.core import TradeIntent
from app.strategies.base import Strategy, StrategyContext
from app.strategies.indicators import closes_of, sma
from app.strategies.paramtools import require_int, require_number
from app.strategies.registry import register

log = get_logger("strategies.bollinger")


def _population_std(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


@register
class BollingerReversion(Strategy):
    """Long-only lower-band reversion with trend gate."""

    name = "bollinger_reversion"

    def validate_params(self) -> None:
        self._period = require_int(self.params, "period", 20)
        self._num_std = require_number(self.params, "num_std", 2.0)
        self._qty = require_int(self.params, "qty", 1)
        self._min_bandwidth = require_number(self.params, "min_bandwidth", 0.0)
        trend = self.params.get("trend_sma", 200)
        if trend is not None and (
            isinstance(trend, bool) or not isinstance(trend, int) or trend < 2
        ):
            raise ValueError(f"param 'trend_sma' must be an int >= 2 or null, got {trend!r}")
        self._trend_sma = trend
        if self._period < 2:
            raise ValueError(f"param 'period' must be >= 2, got {self._period}")
        if self._num_std <= 0:
            raise ValueError(f"param 'num_std' must be > 0, got {self._num_std}")
        if self._min_bandwidth < 0:
            raise ValueError(f"param 'min_bandwidth' must be >= 0, got {self._min_bandwidth}")
        if self._qty <= 0:
            raise ValueError(f"param 'qty' must be > 0, got {self._qty}")

    def warmup_bars(self) -> int:
        return max(self._period, self._trend_sma or 0) + 1

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        closes = closes_of(ctx.bars)
        needed = max(self._period, self._trend_sma or 0)
        if len(closes) < needed:
            return []
        close = closes[-1]
        middle = sma(closes, self._period)
        if middle is None:
            return []
        std = _population_std(closes[-self._period:])
        lower = middle - self._num_std * std
        upper = middle + self._num_std * std

        position = ctx.position
        is_flat = position is None or position.qty == 0
        is_long = position is not None and position.qty > 0
        detail = {
            "close": f"{close:.4f}", "middle": f"{middle:.4f}",
            "lower": f"{lower:.4f}", "upper": f"{upper:.4f}",
        }

        if is_flat:
            if self._trend_sma is not None:
                trend = sma(closes, self._trend_sma)
                if trend is None or close <= trend:
                    return []
            if (
                self._min_bandwidth > 0
                and middle > 0
                and (upper - lower) / middle < self._min_bandwidth
            ):
                return []
            if close < lower:
                log.debug("bollinger_entry", symbol=ctx.symbol, **detail)
                return [
                    TradeIntent(
                        symbol=ctx.symbol, action=SignalAction.BUY,
                        qty=Decimal(self._qty), context=detail,
                    )
                ]
            return []

        if is_long and close >= middle:
            log.debug("bollinger_exit", symbol=ctx.symbol, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.CLOSE,
                    qty=position.qty, context=detail,
                )
            ]
        return []
