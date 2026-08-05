"""SMA crossover example strategy.

Exists to prove the ``Strategy`` interface end to end, not to make money.
Long-only, no leverage: a fast/slow simple-moving-average crossover on closes.

- Fast SMA crosses ABOVE slow SMA on the latest bar while flat  -> BUY ``qty``.
- Fast SMA crosses BELOW slow SMA on the latest bar while long  -> CLOSE the
  full position.
- Anything else -> no intents.

Crossover detection compares the SMAs on the previous bar against the SMAs on
the latest bar, so at least ``slow + 1`` bars are required (``warmup_bars``).
The strategy is pure and deterministic: Decimal math on closes only, no I/O,
no randomness, no wall-clock reads. Intents are proposals; the risk engine
approves or rejects them downstream.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.db.models import SignalAction
from app.logging import get_logger
from app.schemas.core import TradeIntent
from app.strategies.base import Strategy, StrategyContext
from app.strategies.registry import register

log = get_logger("strategies.sma_cross")


def _require_int(params: dict[str, Any], key: str, default: int) -> int:
    """Fetch an integer param (bools are rejected: JSON ``true`` is not a count)."""
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"param {key!r} must be an integer, got {value!r}")
    return value


def _sma(closes: list[Decimal], n: int) -> Decimal:
    """Simple moving average of the last ``n`` closes (caller guarantees length)."""
    return sum(closes[-n:], Decimal(0)) / Decimal(n)


@register
class SmaCross(Strategy):
    """Long-only fast/slow SMA crossover on closing prices."""

    name = "sma_cross"

    def validate_params(self) -> None:
        fast = _require_int(self.params, "fast", 10)
        slow = _require_int(self.params, "slow", 20)
        qty = _require_int(self.params, "qty", 1)
        if not 0 < fast < slow:
            raise ValueError(f"require 0 < fast < slow, got fast={fast} slow={slow}")
        if qty <= 0:
            raise ValueError(f"param 'qty' must be > 0, got {qty}")
        self._fast = fast
        self._slow = slow
        self._qty = Decimal(qty)

    def warmup_bars(self) -> int:
        return self._slow + 1

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        closes = [bar.close for bar in ctx.bars]
        if len(closes) < self._slow + 1:
            # Callers should respect warmup_bars(), but short input must be safe too.
            return []

        prev = closes[:-1]
        fast_prev, slow_prev = _sma(prev, self._fast), _sma(prev, self._slow)
        fast_now, slow_now = _sma(closes, self._fast), _sma(closes, self._slow)

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        position = ctx.position
        is_flat = position is None or position.qty == 0
        is_long = position is not None and position.qty > 0

        detail = {"fast_sma": str(fast_now), "slow_sma": str(slow_now)}
        if crossed_up and is_flat:
            log.debug("sma_cross_entry", symbol=ctx.symbol, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.BUY, qty=self._qty, context=detail
                )
            ]
        if crossed_down and is_long:
            log.debug("sma_cross_exit", symbol=ctx.symbol, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.CLOSE, qty=position.qty, context=detail
                )
            ]
        return []
