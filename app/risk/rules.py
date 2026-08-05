"""Pure risk limit rules.

Each rule is a small, side-effect-free predicate over ``(TradeIntent,
RiskContext)``. Rules never touch the database, the clock, or global settings:
every limit is captured from ``Settings`` at construction time, so a rule's
behavior is fully determined by its inputs. The engine (``app.risk.engine``)
owns ordering, persistence of ``RuleResult``s as ``RiskEvent`` rows, and the
DB-backed halt checks (kill switch, circuit breaker, live gate, duplicates).

Conventions shared by every rule here:

- Verdicts are only APPROVED or REJECTED — HALTED is reserved for the engine's
  operational halt checks.
- Fail closed: if a rule cannot compute its check (no price, no timestamp), it
  REJECTS rather than approving on missing data.
- ``detail`` dicts contain only JSON-serializable values (Decimals are
  stringified) because the engine persists them into a JSON column.
- ``close`` action semantics: a close is treated as a sell of the entire
  currently held long position in the symbol. It is therefore always
  risk-reducing for the limit rules (projected position is flat). If the
  position is already flat, the effective quantity is 0 and QtySanityRule
  rejects it (NoShortSellRule would also never approve selling what is not
  held) — downstream sizing of a flat close must not reach the broker.
"""

from __future__ import annotations

from decimal import Decimal

from app.config import Settings
from app.db.models import RiskVerdict, SignalAction
from app.risk.types import RiskContext, RiskRule, RuleResult
from app.schemas.core import PositionState, TradeIntent

_ZERO = Decimal(0)

# Hard cap on shares per order, independent of notional limits. Deliberately
# not configurable: it is a fat-finger guard of last resort.
MAX_ORDER_SHARES = Decimal(10_000)


# ---------------------------------------------------------------- helpers


def _position_for(ctx: RiskContext, symbol: str) -> PositionState | None:
    for pos in ctx.positions:
        if pos.symbol == symbol:
            return pos
    return None


def _current_qty(ctx: RiskContext, symbol: str) -> Decimal:
    pos = _position_for(ctx, symbol)
    return pos.qty if pos is not None else _ZERO


def _effective_qty(intent: TradeIntent, ctx: RiskContext) -> Decimal:
    """Share quantity this intent would actually trade. For buy/sell it is the
    stated qty; for close it is the full currently held long quantity."""
    if intent.action == SignalAction.CLOSE:
        return max(_current_qty(ctx, intent.symbol), _ZERO)
    return intent.qty


def _signed_qty_delta(intent: TradeIntent, ctx: RiskContext) -> Decimal:
    """Signed change to the symbol's position: buys add, sells/closes remove."""
    qty = _effective_qty(intent, ctx)
    return qty if intent.action == SignalAction.BUY else -qty


def _reference_price(intent: TradeIntent, ctx: RiskContext) -> Decimal | None:
    """Price used for notional math: the limit price when present, else the
    latest observed market price. None means we cannot price the order."""
    return intent.limit_price if intent.limit_price is not None else ctx.last_price


def _approve(rule: str, reason: str, **detail) -> RuleResult:
    return RuleResult(rule=rule, verdict=RiskVerdict.APPROVED, reason=reason, detail=detail)


def _reject(rule: str, reason: str, **detail) -> RuleResult:
    return RuleResult(rule=rule, verdict=RiskVerdict.REJECTED, reason=reason, detail=detail)


# ---------------------------------------------------------------- rules


class QtySanityRule(RiskRule):
    """Order quantity must be a positive, finite number of shares under the
    hard per-order share cap. For ``close``, the effective quantity is the held
    long quantity — a close while flat is rejected here (nothing to close)."""

    name = "qty_sanity"

    def __init__(self, settings: Settings) -> None:
        self.max_shares = MAX_ORDER_SHARES

    def check(self, intent: TradeIntent, ctx: RiskContext) -> RuleResult:
        qty = _effective_qty(intent, ctx)
        if not qty.is_finite():
            return _reject(self.name, f"order qty {qty} is not a finite number", qty=str(qty))
        if qty <= _ZERO:
            if intent.action == SignalAction.CLOSE:
                return _reject(
                    self.name,
                    f"close for {intent.symbol} with no long position (effective qty {qty})",
                    qty=str(qty),
                )
            return _reject(self.name, f"order qty {qty} must be positive", qty=str(qty))
        if qty > self.max_shares:
            return _reject(
                self.name,
                f"order qty {qty} exceeds hard cap of {self.max_shares} shares",
                qty=str(qty),
                max_shares=str(self.max_shares),
            )
        return _approve(self.name, f"qty {qty} within sanity bounds", qty=str(qty))


class SymbolAllowlistRule(RiskRule):
    """If an allowlist is configured, only listed symbols may trade. An empty
    allowlist means any equity symbol is allowed."""

    name = "symbol_allowlist"

    def __init__(self, settings: Settings) -> None:
        self.allowlist = settings.symbol_allowlist()

    def check(self, intent: TradeIntent, ctx: RiskContext) -> RuleResult:
        if not self.allowlist:
            return _approve(self.name, "no allowlist configured; all symbols permitted")
        symbol = intent.symbol.upper()
        if symbol in self.allowlist:
            return _approve(
                self.name, f"{symbol} is in the allowlist", allowlist=sorted(self.allowlist)
            )
        return _reject(
            self.name,
            f"{symbol} is not in the configured symbol allowlist",
            symbol=symbol,
            allowlist=sorted(self.allowlist),
        )


class StaleDataRule(RiskRule):
    """No price, no trade. The latest observed price must exist and be newer
    than ``risk_max_data_age_seconds`` before evaluation time. Missing price or
    timestamp fails closed."""

    name = "stale_data"

    def __init__(self, settings: Settings) -> None:
        self.max_age_seconds = int(settings.risk_max_data_age_seconds)

    def check(self, intent: TradeIntent, ctx: RiskContext) -> RuleResult:
        if ctx.last_price is None or ctx.last_price_ts is None:
            return _reject(
                self.name,
                f"no market price available for {intent.symbol} (fail closed)",
                has_price=ctx.last_price is not None,
                has_price_ts=ctx.last_price_ts is not None,
            )
        # Every timestamp in this system is UTC by convention, but not every
        # source preserves tzinfo (SQLite drops it on round-trip). Subtracting
        # a naive from an aware datetime raises, and a safety rule that throws
        # takes down the whole evaluation instead of failing closed — so
        # normalize rather than trusting the caller.
        price_ts = ctx.last_price_ts
        if price_ts.tzinfo is None:
            price_ts = price_ts.replace(tzinfo=ctx.now.tzinfo)
        age = (ctx.now - price_ts).total_seconds()
        if age >= self.max_age_seconds:
            return _reject(
                self.name,
                f"market data for {intent.symbol} is {age:.0f}s old "
                f"(limit {self.max_age_seconds}s)",
                age_seconds=age,
                max_age_seconds=self.max_age_seconds,
            )
        return _approve(
            self.name,
            f"market data is {age:.0f}s old",
            age_seconds=age,
            max_age_seconds=self.max_age_seconds,
        )


class OrderNotionalRule(RiskRule):
    """Single-order notional (qty x price) must not exceed the per-order cap.
    Uses the limit price when set, else the last observed price; rejects if the
    order cannot be priced at all.

    Orders that shrink the position are always approved, exactly as
    ``PositionLimitRule`` and ``GrossExposureRule`` do. Without this exemption
    a position that grew beyond the cap could never be closed: every exit would
    itself exceed the per-order limit and be rejected, trapping the account in
    the very exposure the cap exists to prevent. A size cap must never be able
    to block de-risking.
    """

    name = "order_notional"

    def __init__(self, settings: Settings) -> None:
        self.max_notional = Decimal(str(settings.risk_max_order_notional))

    def check(self, intent: TradeIntent, ctx: RiskContext) -> RuleResult:
        current = _current_qty(ctx, intent.symbol)
        projected = current + _signed_qty_delta(intent, ctx)
        if abs(projected) < abs(current):
            return _approve(
                self.name,
                f"risk-reducing order: |{intent.symbol}| position {current} -> {projected}",
                current_qty=str(current),
                projected_qty=str(projected),
            )
        price = _reference_price(intent, ctx)
        if price is None:
            return _reject(
                self.name, f"cannot price order for {intent.symbol}: no price available"
            )
        qty = _effective_qty(intent, ctx)
        notional = qty * price
        if notional > self.max_notional:
            return _reject(
                self.name,
                f"order notional {notional} exceeds max {self.max_notional}",
                notional=str(notional),
                max_notional=str(self.max_notional),
                price=str(price),
                qty=str(qty),
            )
        return _approve(
            self.name,
            f"order notional {notional} within max {self.max_notional}",
            notional=str(notional),
            max_notional=str(self.max_notional),
        )


class PositionLimitRule(RiskRule):
    """Projected absolute position notional in the symbol after this order must
    stay under the per-symbol cap. Orders that shrink the position (sells of a
    long, closes) are always approved — reducing risk must never be blocked,
    even when the existing position already exceeds the cap."""

    name = "position_limit"

    def __init__(self, settings: Settings) -> None:
        self.max_notional = Decimal(str(settings.risk_max_position_notional))

    def check(self, intent: TradeIntent, ctx: RiskContext) -> RuleResult:
        current = _current_qty(ctx, intent.symbol)
        projected = current + _signed_qty_delta(intent, ctx)
        if abs(projected) < abs(current):
            return _approve(
                self.name,
                f"risk-reducing order: |{intent.symbol}| position {current} -> {projected}",
                current_qty=str(current),
                projected_qty=str(projected),
            )
        price = _reference_price(intent, ctx)
        if price is None:
            return _reject(
                self.name,
                f"cannot project position notional for {intent.symbol}: no price available",
            )
        projected_notional = abs(projected) * price
        if projected_notional > self.max_notional:
            return _reject(
                self.name,
                f"projected {intent.symbol} position notional {projected_notional} "
                f"exceeds max {self.max_notional}",
                projected_notional=str(projected_notional),
                max_notional=str(self.max_notional),
                current_qty=str(current),
                projected_qty=str(projected),
            )
        return _approve(
            self.name,
            f"projected position notional {projected_notional} within max {self.max_notional}",
            projected_notional=str(projected_notional),
            max_notional=str(self.max_notional),
        )


class GrossExposureRule(RiskRule):
    """Total gross exposure (sum of absolute position market values) plus this
    order's signed notional delta must stay under the account-wide cap.
    Risk-reducing orders are always approved."""

    name = "gross_exposure"

    def __init__(self, settings: Settings) -> None:
        self.max_exposure = Decimal(str(settings.risk_max_gross_exposure))

    def check(self, intent: TradeIntent, ctx: RiskContext) -> RuleResult:
        gross = sum((abs(p.market_value) for p in ctx.positions), _ZERO)
        current = _current_qty(ctx, intent.symbol)
        delta = _signed_qty_delta(intent, ctx)
        if abs(current + delta) < abs(current):
            return _approve(
                self.name,
                f"risk-reducing order; gross exposure {gross} can only fall",
                gross_exposure=str(gross),
            )
        price = _reference_price(intent, ctx)
        if price is None:
            return _reject(
                self.name,
                f"cannot project gross exposure for {intent.symbol}: no price available",
            )
        projected = gross + delta * price
        if projected > self.max_exposure:
            return _reject(
                self.name,
                f"projected gross exposure {projected} exceeds max {self.max_exposure}",
                gross_exposure=str(gross),
                projected_exposure=str(projected),
                max_exposure=str(self.max_exposure),
            )
        return _approve(
            self.name,
            f"projected gross exposure {projected} within max {self.max_exposure}",
            projected_exposure=str(projected),
            max_exposure=str(self.max_exposure),
        )


class DailyLossRule(RiskRule):
    """Halt new orders for the day once realized+unrealized equity loss since
    session start reaches the daily loss limit. If the session-start equity is
    unknown the rule approves — the execution service always supplies it, and
    the stale-data/notional rules still gate the order."""

    name = "daily_loss"

    def __init__(self, settings: Settings) -> None:
        self.max_daily_loss = Decimal(str(settings.risk_max_daily_loss))

    def check(self, intent: TradeIntent, ctx: RiskContext) -> RuleResult:
        if ctx.day_start_equity is None:
            return _approve(self.name, "no session-start equity recorded; rule not applicable")
        pnl = ctx.account.equity - ctx.day_start_equity
        if pnl <= -self.max_daily_loss:
            return _reject(
                self.name,
                f"daily loss {-pnl} reached limit {self.max_daily_loss}; "
                "no new orders for the day",
                day_pnl=str(pnl),
                day_start_equity=str(ctx.day_start_equity),
                equity=str(ctx.account.equity),
                max_daily_loss=str(self.max_daily_loss),
            )
        return _approve(
            self.name,
            f"day P&L {pnl} within loss limit {self.max_daily_loss}",
            day_pnl=str(pnl),
            max_daily_loss=str(self.max_daily_loss),
        )


class DrawdownRule(RiskRule):
    """Reject new orders once equity has drawn down from its historical peak by
    at least ``risk_max_drawdown_pct`` percent. Unknown peak approves (the
    execution service always supplies it)."""

    name = "drawdown"

    def __init__(self, settings: Settings) -> None:
        self.max_drawdown_pct = Decimal(str(settings.risk_max_drawdown_pct))

    def check(self, intent: TradeIntent, ctx: RiskContext) -> RuleResult:
        peak = ctx.peak_equity
        if peak is None or peak <= _ZERO:
            return _approve(self.name, "no peak equity recorded; rule not applicable")
        drawdown_pct = (peak - ctx.account.equity) / peak * Decimal(100)
        if drawdown_pct >= self.max_drawdown_pct:
            return _reject(
                self.name,
                f"drawdown {drawdown_pct:.2f}% from peak {peak} reached "
                f"limit {self.max_drawdown_pct}%",
                drawdown_pct=str(drawdown_pct),
                peak_equity=str(peak),
                equity=str(ctx.account.equity),
                max_drawdown_pct=str(self.max_drawdown_pct),
            )
        return _approve(
            self.name,
            f"drawdown {drawdown_pct:.2f}% within limit {self.max_drawdown_pct}%",
            drawdown_pct=str(drawdown_pct),
            max_drawdown_pct=str(self.max_drawdown_pct),
        )


class NoShortSellRule(RiskRule):
    """The platform is long-only: a sell (or close) may not take the projected
    position below zero. Buys always pass this rule. A close sells exactly the
    held long quantity, so it projects to flat and passes (a flat close is
    rejected earlier by QtySanityRule)."""

    name = "no_short_sell"

    def __init__(self, settings: Settings) -> None:
        pass

    def check(self, intent: TradeIntent, ctx: RiskContext) -> RuleResult:
        if intent.action == SignalAction.BUY:
            return _approve(self.name, "buy order; short-sale check not applicable")
        current = _current_qty(ctx, intent.symbol)
        projected = current + _signed_qty_delta(intent, ctx)
        if projected < _ZERO:
            return _reject(
                self.name,
                f"sell of {_effective_qty(intent, ctx)} exceeds held long qty {current} "
                f"for {intent.symbol} (long-only; projected {projected})",
                current_qty=str(current),
                projected_qty=str(projected),
            )
        return _approve(
            self.name,
            f"sell keeps {intent.symbol} position non-negative ({current} -> {projected})",
            current_qty=str(current),
            projected_qty=str(projected),
        )


def default_rules(settings: Settings) -> list[RiskRule]:
    """The full ordered rule set. Order is deterministic and goes from cheap
    intrinsic checks to portfolio-level checks; the engine runs all of them and
    persists every result, so ordering never hides a violation."""
    return [
        QtySanityRule(settings),
        SymbolAllowlistRule(settings),
        StaleDataRule(settings),
        OrderNotionalRule(settings),
        PositionLimitRule(settings),
        GrossExposureRule(settings),
        DailyLossRule(settings),
        DrawdownRule(settings),
        NoShortSellRule(settings),
    ]
