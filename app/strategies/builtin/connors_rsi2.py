"""Connors RSI-2 pullback — Larry Connors & Cesar Alvarez, "Short Term
Trading Strategies That Work" (2008); RSI per Wilder (1978).

Canonical rules encoded:

- BUY when close > 200-day SMA (the integral long-term trend gate — below it
  the published strategy shorts, which maps to "no signal" on a long-only
  platform) AND RSI(2) < 10 (Connors' aggressive book variant uses < 5 —
  a params choice).
- Exit, per the book's two published conventions (``exit_mode``):
  ``sma``  — close crosses above its 5-day SMA (canonical for longs);
  ``rsi``  — RSI(2) closes above 70.
- No stop-loss and no profit target: Connors' testing found hard stops hurt
  this system; the platform's risk engine provides the independent guardrails
  instead. Expect many small wins against rare deep losers.

Wilder RSI warm-up: RSI is computed over the full bar window, so values are
deterministic for a given window; ``warmup_bars`` guarantees ample history.
"""

from __future__ import annotations

from decimal import Decimal

from app.db.models import SignalAction
from app.logging import get_logger
from app.schemas.core import TradeIntent
from app.strategies.base import Strategy, StrategyContext
from app.strategies.indicators import closes_of, sma, wilder_rsi
from app.strategies.paramtools import require_int, require_number
from app.strategies.registry import register

log = get_logger("strategies.connors_rsi2")


@register
class ConnorsRsi2(Strategy):
    """Long-only RSI(2) pullback with 200-day SMA trend gate."""

    name = "connors_rsi2"

    def validate_params(self) -> None:
        self._rsi_period = require_int(self.params, "rsi_period", 2)
        self._entry_threshold = require_number(self.params, "entry_threshold", 10.0)
        self._trend_sma = require_int(self.params, "trend_sma", 200)
        self._exit_sma = require_int(self.params, "exit_sma", 5)
        self._exit_rsi = require_number(self.params, "exit_rsi", 70.0)
        self._qty = require_int(self.params, "qty", 1)
        exit_mode = self.params.get("exit_mode", "sma")
        if exit_mode not in ("sma", "rsi"):
            raise ValueError(f"param 'exit_mode' must be sma|rsi, got {exit_mode!r}")
        self._exit_mode = exit_mode
        if self._rsi_period < 1:
            raise ValueError(f"param 'rsi_period' must be >= 1, got {self._rsi_period}")
        if not 0 < self._entry_threshold < 50:
            raise ValueError(
                f"param 'entry_threshold' must be in (0, 50), got {self._entry_threshold}"
            )
        if not 50 < self._exit_rsi < 100:
            raise ValueError(f"param 'exit_rsi' must be in (50, 100), got {self._exit_rsi}")
        if self._trend_sma < 2 or self._exit_sma < 2:
            raise ValueError("trend_sma and exit_sma must both be >= 2")
        if self._qty <= 0:
            raise ValueError(f"param 'qty' must be > 0, got {self._qty}")

    def warmup_bars(self) -> int:
        return self._trend_sma + 1

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        closes = closes_of(ctx.bars)
        if len(closes) < self._trend_sma:
            return []
        close = closes[-1]
        rsi = wilder_rsi(closes, self._rsi_period)
        trend = sma(closes, self._trend_sma)
        if rsi is None or trend is None:
            return []

        position = ctx.position
        is_flat = position is None or position.qty == 0
        is_long = position is not None and position.qty > 0
        detail = {"rsi": f"{rsi:.2f}", "trend_sma": f"{trend:.4f}", "close": f"{close:.4f}"}

        if is_flat:
            if close > trend and rsi < self._entry_threshold:
                log.debug("connors_rsi2_entry", symbol=ctx.symbol, **detail)
                return [
                    TradeIntent(
                        symbol=ctx.symbol, action=SignalAction.BUY,
                        qty=Decimal(self._qty), context=detail,
                    )
                ]
            return []

        if not is_long:
            return []
        if self._exit_mode == "sma":
            exit_line = sma(closes, self._exit_sma)
            should_exit = exit_line is not None and close > exit_line
            detail["exit_sma"] = f"{exit_line:.4f}" if exit_line is not None else "n/a"
        else:
            should_exit = rsi > self._exit_rsi
        if should_exit:
            log.debug("connors_rsi2_exit", symbol=ctx.symbol, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.CLOSE,
                    qty=position.qty, context=detail,
                )
            ]
        return []
