"""Mark Minervini's Trend Template (SEPA) — the Stage 2 uptrend screen.

The eight published criteria, all of which must pass simultaneously (failing
one eliminates the stock regardless of its other merits):

1. Price above both the 150-day and 200-day moving averages.
2. The 150-day MA is above the 200-day MA.
3. The 200-day MA is trending up for at least 1 month (~21 trading days).
4. The 50-day MA is above both the 150-day and 200-day MAs.
5. Price is above the 50-day MA.
6. Price is at least 25% above its 52-week low (30% in *Trade Like a Stock
   Market Wizard* — ``low_multiple``).
7. Price is within 25% of its 52-week high.
8. Relative Strength ranking ≥ 70 (as reported by IBD).

**Criterion 8 is an approximation and is flagged as such at runtime.** IBD's
RS rating is a 1–99 percentile against the entire market; a single-symbol
platform cannot compute a universe percentile. This implements the *intent* —
outperformance — as trailing return minus the benchmark's over the same
window (see ``app.strategies.benchmark``). When no benchmark is available the
criterion is **skipped** and every emitted intent carries
``rs_skipped: "true"`` so the decision audit records that the screen ran
weaker than specified. Set ``require_rs_data`` to refuse entries instead.

**Scope note**: the Trend Template is a *screen* — it identifies stocks in a
Stage 2 uptrend. Minervini's actual entries are VCP breakouts and his exits
are stop-driven. Using the template itself as the entry/exit rule (as here) is
a documented extension, not his complete system. ``exit_mode`` selects between
exiting when the template breaks (default) and the softer 50-day MA break.
"""

from __future__ import annotations

from decimal import Decimal

from app.db.models import SignalAction
from app.logging import get_logger
from app.schemas.core import TradeIntent
from app.strategies.base import Strategy, StrategyContext
from app.strategies.benchmark import read_benchmark, relative_return
from app.strategies.indicators import closes_of, sma
from app.strategies.paramtools import require_int, require_number
from app.strategies.registry import register

log = get_logger("strategies.minervini")

EXIT_MODES = ("template_fail", "sma_fast")


@register
class MinerviniTrendTemplate(Strategy):
    """Long-only Stage 2 trend template."""

    name = "minervini_trend_template"

    def validate_params(self) -> None:
        self._sma_fast = require_int(self.params, "sma_fast", 50)
        self._sma_mid = require_int(self.params, "sma_mid", 150)
        self._sma_slow = require_int(self.params, "sma_slow", 200)
        self._slow_rising_days = require_int(self.params, "slow_rising_days", 21)
        self._window_52w = require_int(self.params, "window_52w", 252)
        self._low_multiple = require_number(self.params, "low_multiple", 1.25)
        self._high_ratio = require_number(self.params, "high_ratio", 0.75)
        self._rs_lookback = self.params.get("rs_lookback", "return_12m")
        self._require_rs_data = bool(self.params.get("require_rs_data", False))
        self._qty = require_int(self.params, "qty", 1)
        exit_mode = self.params.get("exit_mode", "template_fail")
        if exit_mode not in EXIT_MODES:
            raise ValueError(f"param 'exit_mode' must be one of {EXIT_MODES}, got {exit_mode!r}")
        self._exit_mode = exit_mode
        if not 0 < self._sma_fast < self._sma_mid < self._sma_slow:
            raise ValueError(
                "require 0 < sma_fast < sma_mid < sma_slow, got "
                f"{self._sma_fast}/{self._sma_mid}/{self._sma_slow}"
            )
        if self._slow_rising_days < 1:
            raise ValueError(
                f"param 'slow_rising_days' must be >= 1, got {self._slow_rising_days}"
            )
        if self._window_52w < 2:
            raise ValueError(f"param 'window_52w' must be >= 2, got {self._window_52w}")
        if self._low_multiple < 1:
            raise ValueError(f"param 'low_multiple' must be >= 1, got {self._low_multiple}")
        if not 0 < self._high_ratio <= 1:
            raise ValueError(f"param 'high_ratio' must be in (0, 1], got {self._high_ratio}")
        if not isinstance(self._rs_lookback, str) or not self._rs_lookback:
            raise ValueError(f"param 'rs_lookback' must be a string, got {self._rs_lookback!r}")
        if self._qty <= 0:
            raise ValueError(f"param 'qty' must be > 0, got {self._qty}")

    def warmup_bars(self) -> int:
        return max(self._sma_slow + self._slow_rising_days, self._window_52w) + 1

    # ------------------------------------------------------------ evaluation

    def evaluate(self, ctx: StrategyContext) -> tuple[dict[str, bool], dict[str, str]] | None:
        """Evaluate all eight criteria. Returns (results, detail) or None when
        there is not enough history to judge."""
        closes = closes_of(ctx.bars)
        if len(closes) < self.warmup_bars() - 1:
            return None
        fast = sma(closes, self._sma_fast)
        mid = sma(closes, self._sma_mid)
        slow = sma(closes, self._sma_slow)
        slow_prior = sma(closes[: -self._slow_rising_days], self._sma_slow)
        if None in (fast, mid, slow, slow_prior):
            return None

        close = closes[-1]
        window = closes[-self._window_52w:]
        low_52w, high_52w = min(window), max(window)

        results = {
            "c1_above_mid_and_slow": close > mid and close > slow,
            "c2_mid_above_slow": mid > slow,
            "c3_slow_rising": slow > slow_prior,
            "c4_fast_above_mid_and_slow": fast > mid and fast > slow,
            "c5_above_fast": close > fast,
            "c6_above_52w_low": close >= low_52w * self._low_multiple,
            "c7_near_52w_high": close >= high_52w * self._high_ratio,
        }
        detail = {
            "close": f"{close:.4f}",
            f"sma{self._sma_fast}": f"{fast:.4f}",
            f"sma{self._sma_mid}": f"{mid:.4f}",
            f"sma{self._sma_slow}": f"{slow:.4f}",
            "low_52w": f"{low_52w:.4f}",
            "high_52w": f"{high_52w:.4f}",
        }

        # Criterion 8: outperformance proxy for IBD's RS percentile.
        rel = relative_return(ctx.bars, read_benchmark(ctx.features), self._rs_lookback)
        if rel is None:
            # No benchmark: skip the criterion, but never silently — the flag
            # travels into the Decision audit row.
            results["c8_relative_strength"] = not self._require_rs_data
            detail["rs_skipped"] = "true"
        else:
            results["c8_relative_strength"] = rel > 0
            detail["relative_return"] = f"{rel:.6f}"
        return results, detail

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        evaluated = self.evaluate(ctx)
        if evaluated is None:
            return []
        results, detail = evaluated
        passes = all(results.values())
        failed = [name for name, ok in results.items() if not ok]
        detail["criteria_passed"] = str(sum(results.values()))
        if failed:
            detail["failed"] = ",".join(failed)

        position = ctx.position
        is_flat = position is None or position.qty == 0
        is_long = position is not None and position.qty > 0

        if is_flat:
            if passes:
                log.debug("minervini_entry", symbol=ctx.symbol, **detail)
                return [
                    TradeIntent(
                        symbol=ctx.symbol, action=SignalAction.BUY,
                        qty=Decimal(self._qty), context=detail,
                    )
                ]
            return []

        if not is_long:
            return []

        if self._exit_mode == "template_fail":
            should_exit = not passes
            reason = "template_fail"
        else:  # sma_fast — softer, lets a stock cool without leaving Stage 2
            should_exit = not results["c5_above_fast"]
            reason = "below_sma_fast"
        if should_exit:
            log.debug("minervini_exit", symbol=ctx.symbol, exit_reason=reason, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.CLOSE, qty=position.qty,
                    context={**detail, "exit_reason": reason},
                )
            ]
        return []
