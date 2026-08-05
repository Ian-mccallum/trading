"""Wilder-RSI mean-reversion example strategy.

Exists to prove the ``Strategy`` interface end to end, not to make money.
Long-only, no leverage: classic Wilder RSI over closing prices.

- RSI below ``oversold`` while flat  -> BUY ``qty``.
- RSI above ``overbought`` while long -> CLOSE the full position.
- Anything else -> no intents.

Wilder RSI seeds the average gain/loss with a simple mean over the first
``period`` deltas and then applies Wilder smoothing
(``avg = (avg * (period - 1) + latest) / period``) across the remaining bars,
so results are deterministic for a given bar window. At least ``period + 1``
closes are required (``warmup_bars``). Degenerate windows resolve
conventionally: all-gain -> RSI 100, all-loss -> RSI 0, perfectly flat -> a
neutral 50 (no action). Pure Decimal math; no I/O, no randomness.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.db.models import SignalAction
from app.logging import get_logger
from app.schemas.core import TradeIntent
from app.strategies.base import Strategy, StrategyContext
from app.strategies.registry import register

log = get_logger("strategies.rsi_reversion")

_HUNDRED = Decimal(100)


def _require_int(params: dict[str, Any], key: str, default: int) -> int:
    """Fetch an integer param (bools are rejected: JSON ``true`` is not a count)."""
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"param {key!r} must be an integer, got {value!r}")
    return value


def _require_number(params: dict[str, Any], key: str, default: float) -> Decimal:
    """Fetch a numeric threshold param as Decimal (bools rejected)."""
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"param {key!r} must be a number, got {value!r}")
    return Decimal(str(value))


def _wilder_rsi(closes: list[Decimal], period: int) -> Decimal | None:
    """Wilder RSI of the final close, or None if there are too few closes."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else Decimal(0) for d in deltas]
    losses = [-d if d < 0 else Decimal(0) for d in deltas]

    p = Decimal(period)
    avg_gain = sum(gains[:period], Decimal(0)) / p
    avg_loss = sum(losses[:period], Decimal(0)) / p
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (p - 1) + gain) / p
        avg_loss = (avg_loss * (p - 1) + loss) / p

    if avg_loss == 0:
        return _HUNDRED if avg_gain > 0 else Decimal(50)
    rs = avg_gain / avg_loss
    return _HUNDRED - _HUNDRED / (Decimal(1) + rs)


@register
class RsiReversion(Strategy):
    """Long-only Wilder-RSI mean reversion on closing prices."""

    name = "rsi_reversion"

    def validate_params(self) -> None:
        period = _require_int(self.params, "period", 14)
        qty = _require_int(self.params, "qty", 1)
        oversold = _require_number(self.params, "oversold", 30)
        overbought = _require_number(self.params, "overbought", 70)
        if period < 2:
            raise ValueError(f"param 'period' must be >= 2, got {period}")
        if qty <= 0:
            raise ValueError(f"param 'qty' must be > 0, got {qty}")
        if not 0 < oversold < overbought < 100:
            raise ValueError(
                f"require 0 < oversold < overbought < 100, "
                f"got oversold={oversold} overbought={overbought}"
            )
        self._period = period
        self._qty = Decimal(qty)
        self._oversold = oversold
        self._overbought = overbought

    def warmup_bars(self) -> int:
        return self._period + 1

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        closes = [bar.close for bar in ctx.bars]
        rsi = _wilder_rsi(closes, self._period)
        if rsi is None:
            # Callers should respect warmup_bars(), but short input must be safe too.
            return []

        position = ctx.position
        is_flat = position is None or position.qty == 0
        is_long = position is not None and position.qty > 0

        detail = {"rsi": str(rsi)}
        if rsi < self._oversold and is_flat:
            log.debug("rsi_reversion_entry", symbol=ctx.symbol, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.BUY, qty=self._qty, context=detail
                )
            ]
        if rsi > self._overbought and is_long:
            log.debug("rsi_reversion_exit", symbol=ctx.symbol, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.CLOSE, qty=position.qty, context=detail
                )
            ]
        return []
