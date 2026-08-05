"""Risk engine orchestration.

``RiskEngine.evaluate`` is the single choke point every proposed trade passes
through. It runs two layers:

1. DB-backed halt checks the engine performs itself (kill switch, circuit
   breaker — including auto-trip on accumulated broker errors — live-arming,
   and duplicate-order detection against the orders table). These cannot be
   influenced by the caller-supplied context.
2. Pure limit rules from ``app.risk.rules`` (position/exposure/notional
   limits, daily loss, drawdown, stale data, symbol allowlist), which read
   only the immutable ``RiskContext``.

Every rule outcome is persisted as a ``RiskEvent``. There is no bypass flag:
the engine has no parameter that skips checks, and the execution service has
no path to a broker that does not call ``evaluate`` first.
"""

from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import DecisionMode, Environment, Order, OrderStatus, RiskEvent, RiskVerdict
from app.logging import get_logger
from app.risk import state as controls
from app.risk.types import RecentOrder, RiskContext, RiskDecision, RiskRule, RuleResult
from app.schemas.core import TradeIntent

log = get_logger("risk.engine")


class RiskEngine:
    def __init__(self, settings: Settings, rules: list[RiskRule] | None = None) -> None:
        self.settings = settings
        if rules is None:
            from app.risk.rules import default_rules  # deferred: rules import types

            rules = default_rules(settings)
        self.rules = list(rules)

    async def evaluate(
        self,
        session: AsyncSession,
        intent: TradeIntent,
        ctx: RiskContext,
        decision_id=None,
    ) -> RiskDecision:
        results: list[RuleResult] = []

        halt = await self._halt_checks(session, intent, ctx)
        results.extend(halt)
        halted = [r for r in results if r.verdict == RiskVerdict.HALTED]
        rejected = [r for r in results if r.verdict == RiskVerdict.REJECTED]

        if not halted and not rejected:
            for rule in self.rules:
                result = rule.check(intent, ctx)
                results.append(result)

        verdict = self._aggregate(results)
        await self._persist(session, results, ctx.environment, decision_id)

        if verdict != RiskVerdict.APPROVED:
            log.warning(
                "risk_rejected",
                symbol=intent.symbol,
                action=str(intent.action),
                verdict=str(verdict),
                reasons=[r.reason for r in results if not r.approved],
            )
        return RiskDecision(verdict=verdict, results=results)

    # ------------------------------------------------------------ internals

    async def _halt_checks(
        self, session: AsyncSession, intent: TradeIntent, ctx: RiskContext
    ) -> list[RuleResult]:
        results: list[RuleResult] = []

        engaged, reason = await controls.kill_switch_engaged(session)
        if engaged:
            results.append(
                RuleResult(
                    rule="kill_switch",
                    verdict=RiskVerdict.HALTED,
                    reason=f"kill switch engaged: {reason}",
                )
            )
            return results  # nothing else matters

        tripped, breaker_reason = await controls.circuit_breaker_tripped(session, ctx.now)
        if not tripped:
            # Auto-trip on accumulated broker errors.
            errors = await controls.count_recent_broker_errors(
                session,
                ctx.environment,
                self.settings.risk_circuit_breaker_window_seconds,
                ctx.now,
            )
            if errors >= self.settings.risk_circuit_breaker_errors:
                await controls.trip_circuit_breaker(
                    session,
                    reason=f"{errors} broker errors in "
                    f"{self.settings.risk_circuit_breaker_window_seconds}s",
                    environment=ctx.environment,
                    cooldown_seconds=self.settings.risk_circuit_breaker_window_seconds,
                )
                tripped, breaker_reason = True, "auto-tripped on broker errors"
        if tripped:
            results.append(
                RuleResult(
                    rule="circuit_breaker",
                    verdict=RiskVerdict.HALTED,
                    reason=f"circuit breaker tripped: {breaker_reason}",
                )
            )
            return results

        # Live orders require config gates AND the runtime arm switch.
        if intent.mode == DecisionMode.LIVE or ctx.environment == Environment.LIVE:
            if not self.settings.live_trading_allowed():
                results.append(
                    RuleResult(
                        rule="live_gate",
                        verdict=RiskVerdict.HALTED,
                        reason="live trading not enabled by configuration gates",
                    )
                )
                return results
            if not await controls.live_trading_armed(session):
                results.append(
                    RuleResult(
                        rule="live_gate",
                        verdict=RiskVerdict.HALTED,
                        reason="live trading not armed (runtime switch off)",
                    )
                )
                return results

        results.extend(await self._duplicate_check(session, intent, ctx))
        return results

    async def _duplicate_check(
        self, session: AsyncSession, intent: TradeIntent, ctx: RiskContext
    ) -> list[RuleResult]:
        """DB-backed duplicate detection, on two independent grounds.

        1. **Cooldown**: an equivalent order (same symbol + side) created
           inside the configured window.
        2. **Already working**: an equivalent order that is still live at the
           broker, *regardless of age*.

        The second ground is what prevents leverage accumulating on a
        daily-bar platform. An order placed outside market hours stays queued
        for hours; until it fills the broker reports no position, so a
        strategy that only enters "when flat" sees itself as flat and buys
        again on every loop. A 60-second cooldown cannot cover an overnight
        queue, and the result is dozens of stacked entries filling together at
        the open. Age-independent detection is the durable fix.
        """
        window = timedelta(seconds=self.settings.risk_duplicate_window_seconds)
        cutoff = ctx.now - window
        side = "buy" if str(intent.action) == "buy" else "sell"
        stmt = sa.select(Order).where(
            Order.symbol == intent.symbol,
            Order.side == side,
            Order.environment == ctx.environment,
            Order.status.notin_([OrderStatus.REJECTED, OrderStatus.ERROR]),
            sa.or_(
                Order.created_at >= cutoff,
                Order.status.notin_(list(OrderStatus.terminal())),
            ),
        )
        matches = (await session.execute(stmt)).scalars().all()
        if matches:
            working = [o for o in matches if o.status not in OrderStatus.terminal()]
            if working:
                reason = (
                    f"{len(working)} {side} order(s) for {intent.symbol} still "
                    f"working at the broker; waiting for them to fill or cancel"
                )
            else:
                reason = (
                    f"{len(matches)} recent {side} order(s) for {intent.symbol} "
                    f"within {self.settings.risk_duplicate_window_seconds}s"
                )
            return [
                RuleResult(
                    rule="duplicate_order",
                    verdict=RiskVerdict.REJECTED,
                    reason=reason,
                    detail={
                        "recent_count": len(matches),
                        "working_count": len(working),
                    },
                )
            ]
        return []

    @staticmethod
    def _aggregate(results: list[RuleResult]) -> RiskVerdict:
        if any(r.verdict == RiskVerdict.HALTED for r in results):
            return RiskVerdict.HALTED
        if any(r.verdict == RiskVerdict.REJECTED for r in results):
            return RiskVerdict.REJECTED
        return RiskVerdict.APPROVED

    @staticmethod
    async def _persist(
        session: AsyncSession,
        results: list[RuleResult],
        environment: Environment,
        decision_id,
    ) -> None:
        for r in results:
            severity = "info" if r.approved else "warning"
            if r.verdict == RiskVerdict.HALTED:
                severity = "critical"
            session.add(
                RiskEvent(
                    rule=r.rule,
                    verdict=r.verdict,
                    severity=severity,
                    environment=environment,
                    decision_id=decision_id,
                    detail={"reason": r.reason, **r.detail},
                )
            )
        await session.flush()


def build_recent_orders(orders: list[Order]) -> list[RecentOrder]:
    """Helper for callers assembling RiskContext from ORM rows."""
    return [
        RecentOrder(
            symbol=o.symbol,
            side=o.side,
            strategy_id=o.decision.strategy_id if o.decision else None,
            created_at=o.created_at,
            status=o.status,
        )
        for o in orders
    ]
