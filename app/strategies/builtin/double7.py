"""Connors Double 7s — Larry Connors & Cesar Alvarez, "Short Term Trading
Strategies That Work" (2008).

Canonical rules encoded (Alvarez's replication convention):

- BUY when close > 200-day SMA AND today's close is the lowest close of the
  last ``entry_lookback`` (7) trading days — window **includes** today.
- CLOSE when today's close is the highest close of the last ``exit_lookback``
  (7) days. That is the only exit: no stop, no target, no time stop.
- Entry/exit precedence is deterministic: flat → entry check only,
  long → exit check only (the published pitfall: both conditions can hold on
  the same bar in choppy data; a position also keeps making new 7-day lows —
  the flat-check gate prevents re-entry pyramiding).
- The 200-day SMA gates entries only; the published rules do not force an
  exit if price later loses the SMA mid-trade.
"""

from __future__ import annotations

from decimal import Decimal

from app.db.models import SignalAction
from app.logging import get_logger
from app.schemas.core import TradeIntent
from app.strategies.base import Strategy, StrategyContext
from app.strategies.indicators import closes_of, sma
from app.strategies.paramtools import require_int
from app.strategies.registry import register

log = get_logger("strategies.double7")


@register
class ConnorsDouble7(Strategy):
    """Long-only Double 7s: buy 7-day closing lows above the 200-SMA."""

    name = "double7"

    def validate_params(self) -> None:
        self._entry_lookback = require_int(self.params, "entry_lookback", 7)
        self._exit_lookback = require_int(self.params, "exit_lookback", 7)
        self._trend_sma = require_int(self.params, "trend_sma", 200)
        self._qty = require_int(self.params, "qty", 1)
        if self._entry_lookback < 2 or self._exit_lookback < 2:
            raise ValueError("entry_lookback and exit_lookback must both be >= 2")
        if self._trend_sma < 2:
            raise ValueError(f"param 'trend_sma' must be >= 2, got {self._trend_sma}")
        if self._qty <= 0:
            raise ValueError(f"param 'qty' must be > 0, got {self._qty}")

    def warmup_bars(self) -> int:
        return max(self._trend_sma, self._entry_lookback, self._exit_lookback) + 1

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        closes = closes_of(ctx.bars)
        if len(closes) < max(self._trend_sma, self._entry_lookback, self._exit_lookback):
            return []
        close = closes[-1]
        position = ctx.position
        is_flat = position is None or position.qty == 0
        is_long = position is not None and position.qty > 0

        if is_flat:
            trend = sma(closes, self._trend_sma)
            lowest = min(closes[-self._entry_lookback:])  # includes today (canonical)
            if trend is not None and close > trend and close <= lowest:
                detail = {
                    "close": f"{close:.4f}",
                    "lowest_close": f"{lowest:.4f}",
                    "trend_sma": f"{trend:.4f}",
                }
                log.debug("double7_entry", symbol=ctx.symbol, **detail)
                return [
                    TradeIntent(
                        symbol=ctx.symbol, action=SignalAction.BUY,
                        qty=Decimal(self._qty), context=detail,
                    )
                ]
            return []

        if is_long:
            highest = max(closes[-self._exit_lookback:])
            if close >= highest:
                detail = {"close": f"{close:.4f}", "highest_close": f"{highest:.4f}"}
                log.debug("double7_exit", symbol=ctx.symbol, **detail)
                return [
                    TradeIntent(
                        symbol=ctx.symbol, action=SignalAction.CLOSE,
                        qty=position.qty, context=detail,
                    )
                ]
        return []
