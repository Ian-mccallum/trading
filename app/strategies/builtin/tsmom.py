"""Time-Series Momentum (TSMOM) — Moskowitz, Ooi & Pedersen, JFE 2012.

The core trend-following signal of the managed-futures industry (AQR, Man
AHL, Winton): go long when the trailing 12-month return is positive, flat
(canonically short; long-only here) when it is not.

Canonical conventions encoded:

- Lookback 252 trading days (~12 months), **no skip month** — the skip-month
  applies to cross-sectional (Jegadeesh-Titman) momentum, not TSMOM; MOP use
  the full trailing 12 months. ``skip_days`` exists as a param, default 0.
- Monthly evaluation cadence: the signal is only acted on at the first bar of
  a new calendar month, computed from data through the prior (month-end)
  bar. ``eval_frequency='daily'`` enables the common daily generalization.
- Optional volatility-targeting overlay (MOP position scaling / Barroso &
  Santa-Clara risk management): qty is scaled by ``min(1, target/realized)``
  — shrink-only, never levered, using vol estimated through the prior bar.

Simplifications vs the paper (documented in docs/strategies.md): raw return
instead of excess-over-T-bill return; the short leg maps to flat.
"""

from __future__ import annotations

from decimal import Decimal

from app.db.models import SignalAction
from app.logging import get_logger
from app.schemas.core import TradeIntent
from app.strategies.base import Strategy, StrategyContext
from app.strategies.indicators import closes_of, trailing_return, vol_scale
from app.strategies.paramtools import optional_number, require_int, require_number
from app.strategies.registry import register

log = get_logger("strategies.tsmom")


@register
class TimeSeriesMomentum(Strategy):
    """Long-only TSMOM: long while the trailing return is positive."""

    name = "tsmom"

    def validate_params(self) -> None:
        self._lookback = require_int(self.params, "lookback_days", 252)
        self._skip = require_int(self.params, "skip_days", 0)
        self._qty = require_int(self.params, "qty", 1)
        self._dead_band = require_number(self.params, "dead_band", 0.0)
        self._vol_target = optional_number(self.params, "vol_target_annual")
        self._vol_window = require_int(self.params, "vol_window_days", 60)
        frequency = self.params.get("eval_frequency", "monthly")
        if frequency not in ("monthly", "daily"):
            raise ValueError(f"param 'eval_frequency' must be monthly|daily, got {frequency!r}")
        self._monthly = frequency == "monthly"
        if self._lookback <= self._skip or self._lookback < 2:
            raise ValueError(
                f"require lookback_days > skip_days >= 0, got {self._lookback}/{self._skip}"
            )
        if self._qty <= 0:
            raise ValueError(f"param 'qty' must be > 0, got {self._qty}")
        if self._dead_band < 0:
            raise ValueError(f"param 'dead_band' must be >= 0, got {self._dead_band}")
        if self._vol_target is not None and self._vol_target <= 0:
            raise ValueError("param 'vol_target_annual' must be > 0 or null")
        if self._vol_window < 2:
            raise ValueError(f"param 'vol_window_days' must be >= 2, got {self._vol_window}")

    def warmup_bars(self) -> int:
        return self._lookback + 2  # +1 return baseline, +1 for the month-end slice

    def _is_eval_bar(self, ctx: StrategyContext) -> bool:
        if not self._monthly:
            return True
        if len(ctx.bars) < 2:
            return False
        # First bar of a new calendar month: decide on data through month-end.
        return ctx.bars[-1].ts.month != ctx.bars[-2].ts.month

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        if len(ctx.bars) < self.warmup_bars() or not self._is_eval_bar(ctx):
            return []
        closes = closes_of(ctx.bars)
        # Monthly cadence: the signal is the month-end value, i.e. computed
        # through the PREVIOUS bar; daily cadence uses the latest close.
        signal_closes = closes[:-1] if self._monthly else closes
        momentum = trailing_return(signal_closes, self._lookback, self._skip)
        if momentum is None:
            return []

        position = ctx.position
        is_flat = position is None or position.qty == 0
        is_long = position is not None and position.qty > 0
        detail = {"trailing_return": f"{momentum:.6f}", "lookback_days": str(self._lookback)}

        if momentum > self._dead_band and is_flat:
            qty = self._qty
            if self._vol_target is not None:
                scale = vol_scale(signal_closes, self._vol_target, self._vol_window)
                qty = max(1, int(self._qty * scale))
                detail["vol_scale"] = f"{scale:.4f}"
            log.debug("tsmom_entry", symbol=ctx.symbol, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.BUY, qty=Decimal(qty), context=detail
                )
            ]
        if momentum <= 0 and is_long:
            log.debug("tsmom_exit", symbol=ctx.symbol, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol,
                    action=SignalAction.CLOSE,
                    qty=position.qty,
                    context=detail,
                )
            ]
        return []
