"""Daily liquidity-sweep reversal (failed breakdown).

**This is not TJR's strategy, and must never be labelled as one.** TJR's
method is session-based intraday trading: Asia-session liquidity pools swept
during the London and New York opens, fair value gaps, 1m-15m entries. Daily
bars have no sessions and no intraday sweeps, so the structure that defines
that method does not exist here. See docs/investor-personas-research.md §A4.

What this *is*: the same underlying idea — price runs the stops resting below
an obvious low, then fails to hold — expressed on daily bars, where it is a
long-documented standalone pattern. Linda Raschke published it as **Turtle
Soup** in *Street Smarts* (1995), which is where the specific conditions
below come from:

1. Today's low breaks below the lowest low of the prior ``lookback`` bars.
2. That prior low is at least ``min_low_age`` bars old. Raschke's condition:
   a low set only a day or two ago has not accumulated the resting orders
   that make the sweep meaningful.
3. Price closes back **above** the swept low: the breakdown failed.

Exits are bracketed from the entry price, plus an optional time-independent
give-up when price closes below the sweep low (the thesis is simply wrong).
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

log = get_logger("strategies.sweep_reversal")


@register
class SweepReversal(Strategy):
    """Long-only failed-breakdown reversal on daily bars."""

    name = "sweep_reversal"

    def validate_params(self) -> None:
        self._lookback = require_int(self.params, "lookback", 20)
        self._min_low_age = require_int(self.params, "min_low_age", 4)
        self._stop_pct = require_number(self.params, "stop_pct", 0.03)
        self._target_pct = require_number(self.params, "target_pct", 0.06)
        self._trend_sma = self.params.get("trend_sma")
        if self._trend_sma is not None and (
            isinstance(self._trend_sma, bool)
            or not isinstance(self._trend_sma, int)
            or self._trend_sma < 2
        ):
            raise ValueError(
                f"param 'trend_sma' must be an int >= 2 or null, got {self._trend_sma!r}"
            )
        self._give_up_below_sweep = bool(self.params.get("give_up_below_sweep", True))
        self._qty = require_int(self.params, "qty", 1)
        if self._lookback < 5:
            raise ValueError(f"param 'lookback' must be >= 5, got {self._lookback}")
        if not 0 <= self._min_low_age < self._lookback:
            raise ValueError(
                f"require 0 <= min_low_age < lookback, got {self._min_low_age}/{self._lookback}"
            )
        if not 0 < self._stop_pct < 1:
            raise ValueError(f"param 'stop_pct' must be in (0, 1), got {self._stop_pct}")
        if self._target_pct <= 0:
            raise ValueError(f"param 'target_pct' must be > 0, got {self._target_pct}")
        if optional_number(self.params, "max_hold_bars") is not None:
            # Time stops need entry-date state the pure interface does not carry.
            raise ValueError("param 'max_hold_bars' is not supported on a stateless strategy")
        if self._qty <= 0:
            raise ValueError(f"param 'qty' must be > 0, got {self._qty}")

    def warmup_bars(self) -> int:
        return max(self._lookback, self._trend_sma or 0) + 2

    def _swept_low(self, bars: list) -> tuple[float, int] | None:
        """The prior-window low and how many bars ago it occurred."""
        prior = bars[:-1]
        if len(prior) < self._lookback:
            return None
        window = prior[-self._lookback:]
        lows = [float(b.low) for b in window]
        low = min(lows)
        # Age counted from the current bar: the last element of `window` is
        # one bar back, so its age is 1.
        age = len(window) - lows.index(low)
        return low, age

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        bars = ctx.bars
        if len(bars) < self.warmup_bars() - 1:
            return []
        current = bars[-1]
        close, low = float(current.close), float(current.low)
        position = ctx.position
        is_flat = position is None or position.qty == 0
        is_long = position is not None and position.qty > 0

        swept = self._swept_low(bars)
        if swept is None:
            return []
        swept_low, age = swept

        if is_long:
            return self._maybe_exit(ctx, close, position, swept_low)

        if not is_flat:
            return []

        # 1. The stops below the prior low were run.
        if low >= swept_low:
            return []
        # 2. The low is old enough to have accumulated resting orders.
        if age < self._min_low_age:
            return []
        # 3. The breakdown failed: price closed back above the swept level.
        if close <= swept_low:
            return []

        detail = {
            "swept_low": f"{swept_low:.4f}",
            "bar_low": f"{low:.4f}",
            "close": f"{close:.4f}",
            "low_age_bars": str(age),
        }

        if self._trend_sma is not None:
            trend = sma(closes_of(bars), self._trend_sma)
            if trend is None or close <= trend:
                return []
            detail["trend_sma"] = f"{trend:.4f}"

        log.debug("sweep_reversal_entry", symbol=ctx.symbol, **detail)
        return [
            TradeIntent(
                symbol=ctx.symbol, action=SignalAction.BUY,
                qty=Decimal(self._qty), context=detail,
            )
        ]

    def _maybe_exit(
        self, ctx: StrategyContext, close: float, position, swept_low: float
    ) -> list[TradeIntent]:
        entry = float(position.avg_entry_price)
        if entry <= 0:
            return []
        detail = {"close": f"{close:.4f}", "entry": f"{entry:.4f}"}

        if close <= entry * (1 - self._stop_pct):
            detail["exit_reason"] = "stop"
        elif close >= entry * (1 + self._target_pct):
            detail["exit_reason"] = "target"
        elif self._give_up_below_sweep and close < swept_low:
            # The premise was that the breakdown failed. Closing back under the
            # swept level says it did not.
            detail["exit_reason"] = "thesis_invalidated"
            detail["swept_low"] = f"{swept_low:.4f}"
        else:
            return []

        log.debug("sweep_reversal_exit", symbol=ctx.symbol, **detail)
        return [
            TradeIntent(
                symbol=ctx.symbol, action=SignalAction.CLOSE,
                qty=position.qty, context=detail,
            )
        ]
