"""Dual Momentum (GEM) — Gary Antonacci, *Dual Momentum Investing* (2014).

The published rules, monthly on 12-month total returns:

1. **Relative momentum** — compare US equities (S&P 500) against foreign
   equities (ACWI ex-US); select whichever has the higher trailing return.
2. **Absolute momentum** — if that winner also beat cash (T-bills), hold it;
   otherwise hold investment-grade bonds.

Antonacci cites Jegadeesh & Titman and Moskowitz/Ooi/Pedersen for the
12-month lookback, preferring it for out-of-sample support, fewer trades, and
tax efficiency.

**Single-symbol adaptation.** The platform decides one symbol at a time, so
this asks "should this symbol be held?" rather than "which of the pair wins":

- Absolute leg: the symbol's own trailing return must exceed
  ``absolute_floor`` (0 by default; set to a T-bill proxy's return by naming
  a ``cash_symbol``).
- Relative leg: it must also beat ``peer_symbol``'s trailing return, read
  from the peer summaries the worker injects.

Without peer data the relative leg is **skipped and flagged** — at which
point this is plain absolute momentum, equivalent to ``tsmom``. That
degradation is deliberate and visible rather than silent.

The bond leg of GEM is not modelled here: "hold bonds instead" is a
portfolio-level rotation, which the target-weight sleeve expresses. Running
this on an equity symbol alongside a bond sleeve reproduces the intent.
"""

from __future__ import annotations

from decimal import Decimal

from app.db.models import SignalAction
from app.logging import get_logger
from app.schemas.core import TradeIntent
from app.strategies.base import Strategy, StrategyContext
from app.strategies.benchmark import LOOKBACKS, read_peer
from app.strategies.indicators import closes_of, trailing_return
from app.strategies.paramtools import require_int, require_number
from app.strategies.registry import register

log = get_logger("strategies.dual_momentum")


@register
class DualMomentum(Strategy):
    """Long-only dual momentum: absolute plus (optional) relative leg."""

    name = "dual_momentum"

    def validate_params(self) -> None:
        self._lookback_days = require_int(self.params, "lookback_days", 252)
        self._absolute_floor = require_number(self.params, "absolute_floor", 0.0)
        self._qty = require_int(self.params, "qty", 1)
        self._lookback_key = self.params.get("lookback_key", "return_12m")
        for name in ("peer_symbol", "cash_symbol"):
            value = self.params.get(name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"param {name!r} must be a non-empty string or null")
        peer = self.params.get("peer_symbol")
        cash = self.params.get("cash_symbol")
        self._peer_symbol = peer.strip().upper() if isinstance(peer, str) else None
        self._cash_symbol = cash.strip().upper() if isinstance(cash, str) else None
        self._require_peer_data = bool(self.params.get("require_peer_data", False))
        frequency = self.params.get("eval_frequency", "monthly")
        if frequency not in ("monthly", "daily"):
            raise ValueError(f"param 'eval_frequency' must be monthly|daily, got {frequency!r}")
        self._monthly = frequency == "monthly"
        if self._lookback_days < 2:
            raise ValueError(f"param 'lookback_days' must be >= 2, got {self._lookback_days}")
        if self._lookback_key not in LOOKBACKS:
            raise ValueError(
                f"param 'lookback_key' must be one of {sorted(LOOKBACKS)}, "
                f"got {self._lookback_key!r}"
            )
        if self._qty <= 0:
            raise ValueError(f"param 'qty' must be > 0, got {self._qty}")

    def warmup_bars(self) -> int:
        return self._lookback_days + 2

    def _is_eval_bar(self, ctx: StrategyContext) -> bool:
        if not self._monthly:
            return True
        if len(ctx.bars) < 2:
            return False
        return ctx.bars[-1].ts.month != ctx.bars[-2].ts.month

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        if len(ctx.bars) < self.warmup_bars() - 1 or not self._is_eval_bar(ctx):
            return []
        closes = closes_of(ctx.bars)
        own = trailing_return(closes, self._lookback_days)
        if own is None:
            return []

        detail: dict[str, str] = {"own_return": f"{own:.6f}"}

        # --- absolute leg -------------------------------------------------
        floor = self._absolute_floor
        if self._cash_symbol:
            cash = read_peer(ctx.features, self._cash_symbol)
            cash_return = cash.get_return(self._lookback_key) if cash else None
            if cash_return is None:
                detail["cash_reference_skipped"] = "true"
            else:
                floor = cash_return
                detail["cash_return"] = f"{cash_return:.6f}"
        detail["absolute_floor"] = f"{floor:.6f}"
        absolute_ok = own > floor

        # --- relative leg -------------------------------------------------
        relative_ok = True
        if self._peer_symbol:
            peer = read_peer(ctx.features, self._peer_symbol)
            peer_return = peer.get_return(self._lookback_key) if peer else None
            if peer_return is None:
                # No peer data: this reduces to plain absolute momentum. Flag
                # it so the audit row shows the screen ran weaker than named.
                detail["peer_skipped"] = "true"
                detail["peer_symbol"] = self._peer_symbol
                relative_ok = not self._require_peer_data
            else:
                detail["peer_symbol"] = self._peer_symbol
                detail["peer_return"] = f"{peer_return:.6f}"
                relative_ok = own > peer_return

        position = ctx.position
        is_flat = position is None or position.qty == 0
        is_long = position is not None and position.qty > 0
        holds = absolute_ok and relative_ok

        if holds and is_flat:
            log.debug("dual_momentum_entry", symbol=ctx.symbol, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.BUY,
                    qty=Decimal(self._qty), context=detail,
                )
            ]
        if not holds and is_long:
            detail["exit_reason"] = "absolute" if not absolute_ok else "relative"
            log.debug("dual_momentum_exit", symbol=ctx.symbol, **detail)
            return [
                TradeIntent(
                    symbol=ctx.symbol, action=SignalAction.CLOSE,
                    qty=position.qty, context=detail,
                )
            ]
        return []
