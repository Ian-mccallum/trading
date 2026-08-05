"""Position sizing from the owner profile.

Until now every strategy emitted ``qty: 1``, which made backtests and paper
trades meaningless in scale: one $740 share of SPY is 0.7% of a six-figure
account. Strategies should express *direction and conviction*, not portfolio
construction, so sizing belongs here — one policy, applied consistently, and
unit-testable in isolation.

Three rules that keep this safe:

1. **Only entries are sized.** A CLOSE or SELL carries a quantity derived from
   the position that actually exists; rewriting it could try to sell shares
   that are not held. Exits pass through untouched.
2. **Self-sizing strategies are left alone.** A target-weight sleeve already
   computes its quantity from its portfolio weight; re-sizing it would destroy
   the allocation it exists to express.
3. **This can only propose.** Sizing feeds intent construction; the risk
   engine still evaluates every result and remains the final authority. The
   profile can be more conservative than the engine, never less.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from app.db.models import SignalAction
from app.logging import get_logger
from app.profile.model import Profile
from app.schemas.core import TradeIntent

log = get_logger("profile.sizing")


@dataclass(frozen=True)
class SizingResult:
    intent: TradeIntent | None
    reason: str

    @property
    def dropped(self) -> bool:
        return self.intent is None


def size_intent(
    intent: TradeIntent,
    profile: Profile,
    price: Decimal,
    *,
    self_sized: bool = False,
    persona: str | None = None,
) -> SizingResult:
    """Apply the profile's sizing policy to one intent.

    Returns the (possibly re-quantified) intent, or None with a reason when
    the profile declines to trade it at all.
    """
    symbol = intent.symbol.upper()

    if not profile.allows_symbol(symbol):
        return SizingResult(None, f"{symbol} is not tradeable under profile {profile.name!r}")

    # Rule 1: exits keep the quantity derived from the real position.
    if intent.action != SignalAction.BUY:
        return SizingResult(intent, "exit passed through unsized")

    # Rule 2: portfolio legs size themselves.
    if self_sized:
        return SizingResult(intent, "strategy sizes itself")

    if price <= 0:
        return SizingResult(None, "no usable price")

    target = profile.per_position_target() * profile.persona_weight(persona or "")
    ceiling = profile.capital_allocation * profile.max_position_fraction
    notional = min(target, ceiling)

    qty = (notional / price).quantize(Decimal("1"), rounding=ROUND_DOWN)
    if qty <= 0:
        return SizingResult(
            None, f"one share of {symbol} at {price} exceeds the per-position budget {notional}"
        )
    if qty * price < profile.min_order_notional:
        return SizingResult(None, f"order below the {profile.min_order_notional} minimum")

    if qty == intent.qty:
        return SizingResult(intent, "already at target size")

    sized = intent.model_copy(
        update={
            "qty": qty,
            "context": {
                **intent.context,
                "sized_by": "profile",
                "strategy_qty": str(intent.qty),
                "target_notional": f"{notional:.2f}",
            },
        }
    )
    log.debug(
        "intent_sized",
        symbol=symbol,
        from_qty=str(intent.qty),
        to_qty=str(qty),
        notional=f"{notional:.2f}",
        persona=persona,
    )
    return SizingResult(sized, "sized from profile")


def cap_open_positions(
    intents: list[TradeIntent], profile: Profile, open_symbols: set[str]
) -> tuple[list[TradeIntent], list[str]]:
    """Drop entries that would exceed ``max_positions``.

    Exits and adds to existing positions always pass: refusing to close a
    position because a *count* limit was hit would be actively harmful.
    Returns (kept, dropped_reasons).
    """
    kept: list[TradeIntent] = []
    dropped: list[str] = []
    projected = set(open_symbols)

    for intent in intents:
        symbol = intent.symbol.upper()
        is_new_position = intent.action == SignalAction.BUY and symbol not in projected
        if is_new_position and len(projected) >= profile.max_positions:
            dropped.append(
                f"{symbol}: already holding {len(projected)} positions "
                f"(profile limit {profile.max_positions})"
            )
            continue
        if is_new_position:
            projected.add(symbol)
        kept.append(intent)
    return kept, dropped
