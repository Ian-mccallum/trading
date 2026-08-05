"""Donchian channel breakout — Richard Donchian's 4-week rule lineage and the
Turtle Trading systems (Dennis & Eckhardt 1983, published by Curtis Faith).

Canonical conventions encoded:

- The channel is computed over the N bars **preceding** the current bar
  (``exclude_last=True``) — including the current bar is the family's classic
  lookahead bug (a bar can never exceed a channel containing its own high).
- Close-confirmation variant of the intraday tick-through entry (this
  platform trades daily bars; documented approximation).
- Asymmetric exit: lower channel over ``exit_days`` (Turtle S1: 20/10,
  S2: 55/20 — S2 is a params preset, seeded separately).
- Optional Turtle-style hard stop: close below entry − ``stop_atr_mult``·ATR
  (the Turtles' 2N stop), using the position's average entry price.
- Optional Chandelier exit (LeBeau/Elder): close below rolling
  ``chandelier_lookback`` high − ``chandelier_mult``·ATR. Rolling-window
  anchor (StockCharts convention) — the since-entry anchor needs entry-date
  state the pure interface deliberately doesn't carry.
- Optional volatility-targeted sizing (shrink-only, capped at 1×).

Simplification vs the original Turtle rules (see docs/strategies.md):
no pyramiding units and no System-1 last-breakout-winner filter.
"""

from __future__ import annotations

from decimal import Decimal

from app.db.models import SignalAction
from app.logging import get_logger
from app.schemas.core import TradeIntent
from app.strategies.base import Strategy, StrategyContext
from app.strategies.indicators import atr, closes_of, rolling_high, rolling_low, vol_scale
from app.strategies.paramtools import optional_number, require_int
from app.strategies.registry import register

log = get_logger("strategies.donchian")


@register
class DonchianBreakout(Strategy):
    """Long-only Donchian breakout with Turtle-style ATR risk exits."""

    name = "donchian_breakout"

    def validate_params(self) -> None:
        self._entry_days = require_int(self.params, "entry_days", 20)
        self._exit_days = require_int(self.params, "exit_days", 10)
        self._atr_period = require_int(self.params, "atr_period", 20)
        self._qty = require_int(self.params, "qty", 1)
        self._stop_atr_mult = optional_number(self.params, "stop_atr_mult")
        if self._stop_atr_mult is None and "stop_atr_mult" not in self.params:
            self._stop_atr_mult = 2.0  # Turtle 2N default; explicit null disables
        self._chandelier_mult = optional_number(self.params, "chandelier_mult")
        self._chandelier_lookback = require_int(self.params, "chandelier_lookback", 22)
        self._vol_target = optional_number(self.params, "vol_target_annual")
        self._vol_window = require_int(self.params, "vol_window_days", 60)
        if self._entry_days < 2 or self._exit_days < 2:
            raise ValueError("entry_days and exit_days must both be >= 2")
        if self._qty <= 0:
            raise ValueError(f"param 'qty' must be > 0, got {self._qty}")
        if self._atr_period < 1:
            raise ValueError(f"param 'atr_period' must be >= 1, got {self._atr_period}")
        for key, value in (
            ("stop_atr_mult", self._stop_atr_mult),
            ("chandelier_mult", self._chandelier_mult),
        ):
            if value is not None and value <= 0:
                raise ValueError(f"param {key!r} must be > 0 or null, got {value}")

    def warmup_bars(self) -> int:
        return max(self._entry_days, self._exit_days, self._atr_period,
                   self._chandelier_lookback) + 2

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        bars = ctx.bars
        if len(bars) < self._entry_days + 1:
            return []
        close = float(bars[-1].close)
        position = ctx.position
        is_flat = position is None or position.qty == 0
        is_long = position is not None and position.qty > 0

        if is_flat:
            channel_high = rolling_high(bars, self._entry_days)  # excludes current bar
            if channel_high is None or close <= channel_high:
                return []
            detail = {"channel_high": f"{channel_high:.4f}", "close": f"{close:.4f}"}
            qty = self._qty
            if self._vol_target is not None:
                scale = vol_scale(closes_of(bars)[:-1], self._vol_target, self._vol_window)
                qty = max(1, int(self._qty * scale))
                detail["vol_scale"] = f"{scale:.4f}"
            log.debug("donchian_entry", symbol=ctx.symbol, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.BUY, qty=Decimal(qty), context=detail
                )
            ]

        if not is_long:
            return []

        def close_intent(reason: str, **extra: str) -> list[TradeIntent]:
            detail = {"exit_reason": reason, "close": f"{close:.4f}", **extra}
            log.debug("donchian_exit", symbol=ctx.symbol, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.CLOSE,
                    qty=position.qty, context=detail,
                )
            ]

        channel_low = rolling_low(bars, self._exit_days)
        if channel_low is not None and close < channel_low:
            return close_intent("channel_low", channel_low=f"{channel_low:.4f}")

        current_atr = atr(bars, self._atr_period)
        if self._stop_atr_mult is not None and current_atr is not None:
            stop = float(position.avg_entry_price) - self._stop_atr_mult * current_atr
            if close < stop:
                return close_intent("atr_stop", stop=f"{stop:.4f}")

        if self._chandelier_mult is not None and current_atr is not None:
            anchor = rolling_high(bars, self._chandelier_lookback, exclude_last=False)
            if anchor is not None:
                stop = anchor - self._chandelier_mult * current_atr
                if close < stop:
                    return close_intent("chandelier", stop=f"{stop:.4f}")

        return []
