"""52-week-high momentum — George & Hwang, "The 52-Week High and Momentum
Investing", Journal of Finance 2004 (anchoring bias per Kahneman/Tversky).

The published effect is cross-sectional (rank a universe by price / 52-week
high, buy the top 30%). A single-symbol platform cannot rank a universe, so
this encodes the standard practitioner proximity-band adaptation — flagged
as such in docs/strategies.md:

- ratio = close / highest high of the trailing ``window`` (252) bars,
  **including** the current bar (the ratio saturates at 1.0 on a new high;
  no lookahead — only completed bars are ever in ``ctx.bars``).
- BUY when ratio ≥ ``entry_ratio`` (0.95 — "within 5% of the 52-week high").
- CLOSE when ratio ≤ ``exit_ratio`` (0.80). The wide hysteresis gap is
  deliberate: symmetric bands whipsaw badly because the ratio oscillates
  just under 1.0 near highs.
- ``high_basis`` selects the George-Hwang daily-high convention (``high``)
  or the softer max-close variant (``close``).
"""

from __future__ import annotations

from decimal import Decimal

from app.db.models import SignalAction
from app.logging import get_logger
from app.schemas.core import TradeIntent
from app.strategies.base import Strategy, StrategyContext
from app.strategies.indicators import closes_of, rolling_high
from app.strategies.paramtools import require_int, require_number
from app.strategies.registry import register

log = get_logger("strategies.high_52w")


@register
class FiftyTwoWeekHigh(Strategy):
    """Long-only proximity-to-52-week-high momentum with hysteresis bands."""

    name = "high_52w"

    def validate_params(self) -> None:
        self._window = require_int(self.params, "window", 252)
        self._entry_ratio = require_number(self.params, "entry_ratio", 0.95)
        self._exit_ratio = require_number(self.params, "exit_ratio", 0.80)
        self._qty = require_int(self.params, "qty", 1)
        basis = self.params.get("high_basis", "high")
        if basis not in ("high", "close"):
            raise ValueError(f"param 'high_basis' must be high|close, got {basis!r}")
        self._high_basis = basis
        if self._window < 2:
            raise ValueError(f"param 'window' must be >= 2, got {self._window}")
        if not 0 < self._exit_ratio < self._entry_ratio <= 1:
            raise ValueError(
                "require 0 < exit_ratio < entry_ratio <= 1, got "
                f"entry={self._entry_ratio} exit={self._exit_ratio}"
            )
        if self._qty <= 0:
            raise ValueError(f"param 'qty' must be > 0, got {self._qty}")

    def warmup_bars(self) -> int:
        return self._window + 1

    def _reference_high(self, ctx: StrategyContext) -> float | None:
        if self._high_basis == "high":
            return rolling_high(ctx.bars, self._window, exclude_last=False)
        closes = closes_of(ctx.bars)
        if len(closes) < self._window:
            return None
        return max(closes[-self._window:])

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        if len(ctx.bars) < self._window:
            return []
        high = self._reference_high(ctx)
        close = float(ctx.bars[-1].close)
        if high is None or high <= 0:
            return []
        ratio = close / high

        position = ctx.position
        is_flat = position is None or position.qty == 0
        is_long = position is not None and position.qty > 0
        detail = {"ratio": f"{ratio:.4f}", "high": f"{high:.4f}", "close": f"{close:.4f}"}

        if ratio >= self._entry_ratio and is_flat:
            log.debug("high52w_entry", symbol=ctx.symbol, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.BUY,
                    qty=Decimal(self._qty), context=detail,
                )
            ]
        if ratio <= self._exit_ratio and is_long:
            log.debug("high52w_exit", symbol=ctx.symbol, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.CLOSE,
                    qty=position.qty, context=detail,
                )
            ]
        return []
