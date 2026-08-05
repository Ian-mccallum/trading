"""Paul Tudor Jones' 200-day rule.

His documented public heuristic, applied across asset classes:

    "Nothing good happens below the 200-day moving average."

Above it the trend is favorable and he is willing to be long; below it he
turns defensive and cuts size. On a long-only platform that maps exactly:
**long above the 200-day SMA, flat below it.** This is the highest-fidelity
persona in the catalog — the rule is genuinely this simple, and nothing is
lost in translation.

Two documented extensions, both optional and off by default:

- ``stop_pct`` + ``reward_ratio`` — his 5:1 risk/reward discipline expressed
  as a bracket around the entry price. **Interpretation flag**: PTJ's 5:1 is
  a trade-selection and sizing heuristic, not a published mechanical bracket.
  Enabling this is a reasonable encoding of the spirit, not a quotation.
- ``buffer_pct`` — a dead band around the SMA to damp whipsaw when price
  oscillates across the line. A deliberate deviation from the bare rule.

Because it is a pure regime filter, this strategy also doubles as a useful
benchmark: any strategy that cannot beat "long above the 200-day" is not
earning its complexity.
"""

from __future__ import annotations

from decimal import Decimal

from app.db.models import SignalAction
from app.logging import get_logger
from app.schemas.core import TradeIntent
from app.strategies.base import Strategy, StrategyContext
from app.strategies.indicators import closes_of, sma
from app.strategies.paramtools import optional_number, require_int, require_number
from app.strategies.registry import register

log = get_logger("strategies.ptj_trend")


@register
class TrendRegime200(Strategy):
    """Long-only 200-day moving-average regime filter."""

    name = "ptj_trend"

    def validate_params(self) -> None:
        self._sma_period = require_int(self.params, "sma_period", 200)
        self._qty = require_int(self.params, "qty", 1)
        self._buffer_pct = require_number(self.params, "buffer_pct", 0.0)
        self._stop_pct = optional_number(self.params, "stop_pct")
        self._reward_ratio = require_number(self.params, "reward_ratio", 5.0)
        if self._sma_period < 2:
            raise ValueError(f"param 'sma_period' must be >= 2, got {self._sma_period}")
        if self._qty <= 0:
            raise ValueError(f"param 'qty' must be > 0, got {self._qty}")
        if not 0 <= self._buffer_pct < 1:
            raise ValueError(f"param 'buffer_pct' must be in [0, 1), got {self._buffer_pct}")
        if self._stop_pct is not None and not 0 < self._stop_pct < 1:
            raise ValueError(f"param 'stop_pct' must be in (0, 1) or null, got {self._stop_pct}")
        if self._reward_ratio <= 0:
            raise ValueError(f"param 'reward_ratio' must be > 0, got {self._reward_ratio}")

    def warmup_bars(self) -> int:
        return self._sma_period + 1

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        closes = closes_of(ctx.bars)
        trend = sma(closes, self._sma_period)
        if trend is None:
            return []
        close = closes[-1]

        position = ctx.position
        is_flat = position is None or position.qty == 0
        is_long = position is not None and position.qty > 0
        detail = {
            "close": f"{close:.4f}",
            "sma": f"{trend:.4f}",
            "sma_period": str(self._sma_period),
        }

        if is_flat:
            if close > trend * (1 + self._buffer_pct):
                log.debug("ptj_entry", symbol=ctx.symbol, **detail)
                return [
                    TradeIntent(
                        symbol=ctx.symbol, action=SignalAction.BUY,
                        qty=Decimal(self._qty), context=detail,
                    )
                ]
            return []

        if not is_long:
            return []

        def exit_intent(reason: str) -> list[TradeIntent]:
            log.debug("ptj_exit", symbol=ctx.symbol, exit_reason=reason, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.CLOSE, qty=position.qty,
                    context={**detail, "exit_reason": reason},
                )
            ]

        # The rule itself: below the line, get out.
        if close < trend * (1 - self._buffer_pct):
            return exit_intent("below_sma")

        # Optional 5:1 bracket around the entry price (interpretation, not quote).
        if self._stop_pct is not None:
            entry = float(position.avg_entry_price)
            if entry > 0:
                if close <= entry * (1 - self._stop_pct):
                    return exit_intent("stop")
                if close >= entry * (1 + self._stop_pct * self._reward_ratio):
                    return exit_intent("target")

        return []
