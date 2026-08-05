"""Target-weight sleeve — the mechanism that expresses multi-asset portfolios
one symbol at a time.

The platform decides a single symbol per invocation, but portfolio personas
(Dalio's All Weather, Browne's Permanent Portfolio) are defined by weights
across several assets. A sleeve holds *one leg* of such a portfolio: it knows
its own target weight and rebalances toward it when drift exceeds a band.
Run one instance per symbol and the portfolio emerges.

Rebalance bands, not calendars: Browne explicitly advocated rebalancing only
when an asset leaves its band rather than on a schedule.

Two band forms, because portfolios mix large and small legs:

- ``band_rel`` (default 0.25) — a fraction *of the target weight*. Scales
  correctly across legs: All Weather's 7.5% gold sleeve gets a 1.875-point
  band while its 40% bond sleeve gets 10 points.
- ``band_abs`` — an explicit band in portfolio-weight units, overriding
  ``band_rel``. Use this to express Browne's canonical rule exactly:
  target 0.25 with ``band_abs`` 0.10 means "act outside 15–35%".

A fixed absolute band is wrong as a default: 0.10 is wider than a 7.5%
target, so such a leg could never be bought at all.

Weights are fractions of ``ctx.capital_base`` — the capital the owner profile
allocates to the bot — NOT of total account equity. A 25% leg means a quarter
of the money the bot was given, so handing it $25k of a $100k account produces
$6,250 legs rather than $25,000 ones. Without a profile it falls back to
equity, which is correct for backtests.

Fidelity note: legs rebalance independently rather than atomically, which at
daily cadence is an accepted approximation (documented in
docs/spec-persona-engine.md). Long-only throughout: the sleeve buys toward
target, sells down to target, and never goes short.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from app.db.models import SignalAction
from app.logging import get_logger
from app.schemas.core import TradeIntent
from app.strategies.base import Strategy, StrategyContext
from app.strategies.indicators import closes_of, sma
from app.strategies.paramtools import optional_number, require_number
from app.strategies.registry import register

log = get_logger("strategies.target_weight_sleeve")


@register
class TargetWeightSleeve(Strategy):
    """Hold one symbol at a target fraction of equity, within a drift band."""

    name = "target_weight_sleeve"
    #: This IS portfolio construction: the profile sizer must not touch it.
    self_sized = True

    def validate_params(self) -> None:
        # A sleeve is one leg of a portfolio, so it must bind to its own symbol:
        # the strategy loop offers every approved strategy every allowlisted
        # symbol, and an unbound sleeve would try to hold its target weight in
        # each of them. Omitting `symbol` is allowed only for backtests, which
        # drive a single symbol explicitly.
        symbol = self.params.get("symbol")
        if symbol is not None and (not isinstance(symbol, str) or not symbol.strip()):
            raise ValueError(f"param 'symbol' must be a non-empty string or null, got {symbol!r}")
        self._symbol = symbol.strip().upper() if isinstance(symbol, str) else None
        self._target_weight = require_number(self.params, "target_weight", 0.0)
        self._band_abs = optional_number(self.params, "band_abs")
        self._band_rel = require_number(self.params, "band_rel", 0.25)
        self._min_trade_notional = require_number(self.params, "min_trade_notional", 100.0)
        regime = self.params.get("regime_filter_sma")
        if regime is not None and (
            isinstance(regime, bool) or not isinstance(regime, int) or regime < 2
        ):
            raise ValueError(
                f"param 'regime_filter_sma' must be an int >= 2 or null, got {regime!r}"
            )
        self._regime_filter_sma = regime
        if not 0 < self._target_weight <= 1:
            raise ValueError(
                f"param 'target_weight' must be in (0, 1], got {self._target_weight}"
            )
        if self._band_abs is not None and not 0 <= self._band_abs < 1:
            raise ValueError(f"param 'band_abs' must be in [0, 1), got {self._band_abs}")
        if not 0 <= self._band_rel < 1:
            raise ValueError(f"param 'band_rel' must be in [0, 1), got {self._band_rel}")
        if self._min_trade_notional < 0:
            raise ValueError(
                f"param 'min_trade_notional' must be >= 0, got {self._min_trade_notional}"
            )
        # Effective band in portfolio-weight units. An explicit band_abs wins;
        # otherwise the band scales with the target so small legs stay tradeable.
        self._band = (
            self._band_abs
            if self._band_abs is not None
            else self._band_rel * self._target_weight
        )
        if self._band >= self._target_weight:
            raise ValueError(
                f"effective band {self._band:.4f} is >= target_weight "
                f"{self._target_weight:.4f}; this sleeve could never be bought from flat"
            )
        # Guard against a config that can never be satisfied.
        if optional_number(self.params, "max_weight") is not None:
            raise ValueError("param 'max_weight' is not supported; use target_weight + band_abs")

    def warmup_bars(self) -> int:
        return (self._regime_filter_sma or 0) + 1

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        if self._symbol is not None and ctx.symbol.upper() != self._symbol:
            return []  # not this sleeve's leg
        if not ctx.bars or ctx.capital_base <= 0:
            return []
        price = ctx.bars[-1].close
        if price <= 0:
            return []

        position = ctx.position
        held_qty = position.qty if position is not None else Decimal(0)
        held_value = position.market_value if position is not None else Decimal(0)
        if held_qty <= 0:
            held_qty, held_value = Decimal(0), Decimal(0)

        target_weight = Decimal(str(self._target_weight))
        defensive = False
        if self._regime_filter_sma is not None:
            closes = closes_of(ctx.bars)
            trend = sma(closes, self._regime_filter_sma)
            if trend is None:
                return []  # not enough history to judge the regime: do nothing
            if closes[-1] < trend:
                target_weight = Decimal(0)
                defensive = True

        current_weight = held_value / ctx.capital_base
        band = Decimal(str(self._band))
        drift = current_weight - target_weight
        detail = {
            "target_weight": f"{target_weight:.4f}",
            "current_weight": f"{current_weight:.4f}",
            "band": f"{band:.4f}",
            "price": f"{price:.4f}",
        }
        if defensive:
            detail["regime"] = "below_sma_defensive"

        # Defensive exit: flatten completely rather than trimming to zero.
        if target_weight == 0:
            if held_qty > 0:
                log.debug("sleeve_defensive_exit", symbol=ctx.symbol, **detail)
                return [
                    TradeIntent(
                        symbol=ctx.symbol, action=SignalAction.CLOSE,
                        qty=held_qty, context=detail,
                    )
                ]
            return []

        if drift < -band:  # underweight -> buy the shortfall
            notional = (target_weight - current_weight) * ctx.capital_base
            qty = (notional / price).quantize(Decimal("1"), rounding=ROUND_DOWN)
            if qty <= 0 or qty * price < Decimal(str(self._min_trade_notional)):
                return []
            detail["rebalance"] = "buy_to_target"
            log.debug("sleeve_buy", symbol=ctx.symbol, qty=str(qty), **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.BUY, qty=qty, context=detail
                )
            ]

        if drift > band:  # overweight -> sell the excess (never more than held)
            notional = (current_weight - target_weight) * ctx.capital_base
            qty = (notional / price).quantize(Decimal("1"), rounding=ROUND_DOWN)
            qty = min(qty, held_qty)
            if qty <= 0 or qty * price < Decimal(str(self._min_trade_notional)):
                return []
            detail["rebalance"] = "sell_to_target"
            log.debug("sleeve_sell", symbol=ctx.symbol, qty=str(qty), **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.SELL, qty=qty, context=detail
                )
            ]

        return []  # inside the band: deliberately do nothing
