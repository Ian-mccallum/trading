"""O'Neil CAN SLIM — **technical subset only**.

William O'Neil's CAN SLIM is a hybrid fundamental/technical system. This
platform has price and volume, not earnings or institutional ownership, so
three of the seven letters cannot be implemented at all. Naming this class
``ONeilBreakout`` rather than ``CanSlim`` is deliberate: it is the price half
of the method plus O'Neil's risk discipline, and it should never be mistaken
for the whole system.

| Letter | Criterion | Here? |
|---|---|---|
| **C** current quarterly EPS +25% | ❌ needs earnings data |
| **A** annual EPS +25% over 3y | ❌ needs earnings data |
| **N** new high out of a base | ✅ breakout to an N-day high |
| **S** supply/demand: volume surge | ✅ volume ≥ 1.5× average |
| **L** leader not laggard | ⚠️ benchmark-relative return (proxy for RS rank) |
| **I** institutional sponsorship | ❌ needs 13F/ownership data |
| **M** market direction | ✅ benchmark above its own long-term average |

Every emitted intent records which letters were actually evaluated, so the
audit trail never implies more rigor than was applied.

The risk rules are the system's spine and are implemented exactly: cut losses
at 7-8% below the purchase price, take profits around 20-25%. O'Neil's
research is that a sound breakout rarely falls 8% below the pivot before
working, so the stop is tight by design.
"""

from __future__ import annotations

from decimal import Decimal

from app.db.models import SignalAction
from app.logging import get_logger
from app.schemas.core import TradeIntent
from app.strategies.base import Strategy, StrategyContext
from app.strategies.benchmark import read_benchmark, relative_return
from app.strategies.indicators import rolling_high
from app.strategies.paramtools import optional_number, require_int, require_number
from app.strategies.registry import register

log = get_logger("strategies.oneil")


def _average_volume(bars: list, window: int) -> float | None:
    if len(bars) < window + 1:
        return None
    # Exclude the current bar: comparing today's volume against an average
    # that already contains it dilutes exactly the signal being measured.
    volumes = [float(b.volume) for b in bars[-(window + 1):-1]]
    total = sum(volumes)
    return total / len(volumes) if volumes else None


@register
class ONeilBreakout(Strategy):
    """Long-only base breakout with volume confirmation and O'Neil's stops."""

    name = "oneil_breakout"

    def validate_params(self) -> None:
        self._base_lookback = require_int(self.params, "base_lookback", 50)
        self._volume_window = require_int(self.params, "volume_window", 50)
        self._volume_multiple = require_number(self.params, "volume_multiple", 1.5)
        self._stop_pct = require_number(self.params, "stop_pct", 0.08)
        self._target_pct = optional_number(self.params, "target_pct")
        if self._target_pct is None and "target_pct" not in self.params:
            self._target_pct = 0.25  # O'Neil's 20-25%; explicit null disables
        self._require_market_uptrend = bool(self.params.get("require_market_uptrend", True))
        self._require_leader = bool(self.params.get("require_leader", True))
        self._rs_lookback = self.params.get("rs_lookback", "return_3m")
        self._qty = require_int(self.params, "qty", 1)
        if self._base_lookback < 5:
            raise ValueError(f"param 'base_lookback' must be >= 5, got {self._base_lookback}")
        if self._volume_window < 2:
            raise ValueError(f"param 'volume_window' must be >= 2, got {self._volume_window}")
        if self._volume_multiple < 1:
            raise ValueError(
                f"param 'volume_multiple' must be >= 1, got {self._volume_multiple}"
            )
        if not 0 < self._stop_pct < 1:
            raise ValueError(f"param 'stop_pct' must be in (0, 1), got {self._stop_pct}")
        if self._target_pct is not None and self._target_pct <= 0:
            raise ValueError("param 'target_pct' must be > 0 or null")
        if not isinstance(self._rs_lookback, str) or not self._rs_lookback:
            raise ValueError(f"param 'rs_lookback' must be a string, got {self._rs_lookback!r}")
        if self._qty <= 0:
            raise ValueError(f"param 'qty' must be > 0, got {self._qty}")

    def warmup_bars(self) -> int:
        return max(self._base_lookback, self._volume_window) + 2

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        bars = ctx.bars
        if len(bars) < self.warmup_bars() - 1:
            return []
        close = float(bars[-1].close)
        position = ctx.position
        is_flat = position is None or position.qty == 0
        is_long = position is not None and position.qty > 0

        if is_long:
            return self._maybe_exit(ctx, close, position)

        if not is_flat:
            return []

        # N — breakout above the base (window excludes the current bar).
        pivot = rolling_high(bars, self._base_lookback)
        if pivot is None or close <= pivot:
            return []

        detail: dict[str, str] = {
            "pivot": f"{pivot:.4f}",
            "close": f"{close:.4f}",
            "letters_evaluated": "N,S",
        }

        # S — supply and demand: the breakout needs volume behind it.
        average = _average_volume(bars, self._volume_window)
        if average is None or average <= 0:
            return []
        volume = float(bars[-1].volume)
        detail["volume_ratio"] = f"{volume / average:.2f}"
        if volume < average * self._volume_multiple:
            return []

        benchmark = read_benchmark(ctx.features)

        # M — market direction. Skipped (and flagged) without benchmark data.
        if self._require_market_uptrend:
            if benchmark is None or benchmark.above_trend is None:
                detail["market_filter_skipped"] = "true"
            elif not benchmark.above_trend:
                return []
            else:
                detail["letters_evaluated"] += ",M"

        # L — leader, not laggard. A benchmark-relative return, NOT IBD's RS
        # rank: that is a universe percentile no single-symbol platform can
        # compute. Documented approximation.
        if self._require_leader:
            rel = relative_return(bars, benchmark, self._rs_lookback)
            if rel is None:
                detail["leader_check_skipped"] = "true"
            elif rel <= 0:
                return []
            else:
                detail["relative_return"] = f"{rel:.6f}"
                detail["letters_evaluated"] += ",L"

        detail["letters_absent"] = "C,A,I"  # no earnings or ownership data
        log.debug("oneil_entry", symbol=ctx.symbol, **detail)
        return [
            TradeIntent(
                symbol=ctx.symbol, action=SignalAction.BUY,
                qty=Decimal(self._qty), context=detail,
            )
        ]

    def _maybe_exit(self, ctx: StrategyContext, close: float, position) -> list[TradeIntent]:
        entry = float(position.avg_entry_price)
        if entry <= 0:
            return []
        detail = {"close": f"{close:.4f}", "entry": f"{entry:.4f}"}

        # The 7-8% stop is the system's spine, so it is checked before the
        # target: a gap through both must exit as a loss, not a win.
        if close <= entry * (1 - self._stop_pct):
            detail["exit_reason"] = "stop"
            detail["stop"] = f"{entry * (1 - self._stop_pct):.4f}"
        elif self._target_pct is not None and close >= entry * (1 + self._target_pct):
            detail["exit_reason"] = "target"
            detail["target"] = f"{entry * (1 + self._target_pct):.4f}"
        else:
            return []

        log.debug("oneil_exit", symbol=ctx.symbol, **detail)
        return [
            TradeIntent(
                symbol=ctx.symbol, action=SignalAction.CLOSE,
                qty=position.qty, context=detail,
            )
        ]
