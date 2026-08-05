"""Order lifecycle: TradeIntent → Decision → risk evaluation → Order →
broker submission → fill tracking.

Invariants enforced here:

- Every intent becomes a ``Decision`` row (full context preserved) *before*
  any risk evaluation or broker call — nothing trades without an audit row.
- Every intent passes through ``RiskEngine.evaluate``; there is no submission
  path that skips it.
- ``client_order_id`` is derived deterministically from the decision id, so a
  retried submission cannot create a second broker order.
- Shadow-mode decisions are risk-checked and recorded but NEVER reach the
  broker.
- The service refuses to run if the broker's environment does not match the
  configured trading environment.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.base import (
    Broker,
    BrokerError,
    BrokerRejectionError,
    BrokerUnavailableError,
)
from app.config import Settings
from app.db.base import utcnow
from app.db.models import (
    Decision,
    DecisionMode,
    DecisionStatus,
    Environment,
    Fill,
    MarketBar,
    Order,
    OrderStatus,
    PositionSnapshot,
)
from app.logging import get_logger
from app.risk import state as controls
from app.risk.engine import RiskEngine
from app.risk.types import RiskContext
from app.schemas.core import OrderRequest, OrderSide, TradeIntent

log = get_logger("execution.service")


def client_order_id_for(decision_id: uuid.UUID) -> str:
    return f"qp-{decision_id.hex}"


def size_qty(equity: Decimal, price: Decimal, max_notional: Decimal) -> Decimal:
    """Whole-share sizing: as many shares as fit under max_notional (and never
    more than 5% of equity per order)."""
    if price <= 0:
        return Decimal(0)
    cap = min(max_notional, equity * Decimal("0.05"))
    return (cap / price).quantize(Decimal("1"), rounding=ROUND_DOWN)


class ExecutionService:
    def __init__(self, broker: Broker, risk_engine: RiskEngine, settings: Settings) -> None:
        if broker.environment != Environment(settings.trading_env):
            raise RuntimeError(
                f"broker environment {broker.environment} does not match "
                f"configured trading_env {settings.trading_env}; refusing to start"
            )
        self.broker = broker
        self.risk_engine = risk_engine
        self.settings = settings

    # ------------------------------------------------------------ intake

    async def execute_intent(self, session: AsyncSession, intent: TradeIntent) -> Decision:
        environment = Environment(self.settings.trading_env)
        if intent.mode == DecisionMode.LIVE and environment != Environment.LIVE:
            # A live-mode intent in a paper deployment is downgraded, loudly.
            log.warning("live_intent_in_paper_env_downgraded", symbol=intent.symbol)
            intent = intent.model_copy(update={"mode": DecisionMode.PAPER})

        decision = Decision(
            mode=intent.mode,
            environment=environment,
            status=DecisionStatus.PROPOSED,
            strategy_id=intent.strategy_id,
            model_version_id=intent.model_version_id,
            signal_id=intent.signal_id,
            symbol=intent.symbol,
            action=intent.action,
            qty=intent.qty,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            context=dict(intent.context),
        )
        session.add(decision)
        await session.flush()

        # Building the context talks to the broker, so it can fail. Resolve the
        # decision to FAILED rather than letting the exception escape and leave
        # an orphaned PROPOSED row: every decision must reach a terminal state,
        # or the audit spine accumulates rows nothing ever cleans up.
        try:
            ctx = await self._build_risk_context(session, intent, environment)
        except BrokerError as exc:
            decision.status = DecisionStatus.FAILED
            decision.risk_detail = {"error": "risk_context_unavailable", "detail": str(exc)[:500]}
            await controls.record_broker_error(
                session, environment, {"error": str(exc), "kind": "risk_context"}
            )
            await session.flush()
            log.warning("risk_context_failed", symbol=intent.symbol, error=str(exc))
            return decision

        risk = await self.risk_engine.evaluate(session, intent, ctx, decision_id=decision.id)
        decision.risk_verdict = risk.verdict
        decision.risk_detail = {
            "results": [
                {"rule": r.rule, "verdict": r.verdict, "reason": r.reason} for r in risk.results
            ]
        }

        if not risk.approved:
            decision.status = DecisionStatus.REJECTED
            await session.flush()
            return decision

        decision.status = DecisionStatus.APPROVED
        if intent.mode == DecisionMode.SHADOW:
            decision.outcome = {
                "shadow": True,
                "reference_price": str(ctx.last_price) if ctx.last_price is not None else None,
            }
            await session.flush()
            return decision

        await self._submit(session, decision, intent, environment)
        return decision

    # ------------------------------------------------------------ submission

    async def _submit(
        self,
        session: AsyncSession,
        decision: Decision,
        intent: TradeIntent,
        environment: Environment,
    ) -> None:
        side = OrderSide.BUY if str(intent.action) == "buy" else OrderSide.SELL
        order = Order(
            decision_id=decision.id,
            client_order_id=client_order_id_for(decision.id),
            environment=environment,
            symbol=intent.symbol,
            side=side,
            qty=intent.qty,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            status=OrderStatus.PENDING_SUBMIT,
        )
        session.add(order)
        await session.flush()

        request = OrderRequest(
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=side,
            qty=order.qty,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
        )
        try:
            state = await self.broker.submit_order(request)
        except BrokerRejectionError as exc:
            order.status = OrderStatus.REJECTED
            order.status_reason = str(exc)[:500]
            decision.status = DecisionStatus.FAILED
            await controls.record_broker_error(
                session, environment, {"error": str(exc), "kind": "rejection",
                                       "client_order_id": order.client_order_id}
            )
            await session.flush()
            return
        except BrokerUnavailableError as exc:
            # The order may or may not have landed; reconciliation resolves it.
            order.status = OrderStatus.ERROR
            order.status_reason = str(exc)[:500]
            decision.status = DecisionStatus.FAILED
            await controls.record_broker_error(
                session, environment, {"error": str(exc), "kind": "unavailable",
                                       "client_order_id": order.client_order_id}
            )
            await session.flush()
            return

        order.broker_order_id = state.broker_order_id
        order.status = state.status
        order.filled_qty = state.filled_qty
        order.filled_avg_price = state.filled_avg_price
        order.submitted_at = state.submitted_at or utcnow()
        order.raw_broker_payload = state.raw
        decision.status = DecisionStatus.EXECUTED
        await session.flush()
        log.info(
            "order_submitted",
            symbol=order.symbol,
            side=str(side),
            qty=str(order.qty),
            client_order_id=order.client_order_id,
            environment=environment,
        )

    # ------------------------------------------------------------ context

    async def _build_risk_context(
        self, session: AsyncSession, intent: TradeIntent, environment: Environment
    ) -> RiskContext:
        account = await self.broker.get_account()
        positions = await self.broker.get_positions()

        bar_row = (
            await session.execute(
                sa.select(MarketBar)
                .where(MarketBar.symbol == intent.symbol)
                .order_by(MarketBar.ts.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        last_price = bar_row.close if bar_row else None
        last_price_ts = bar_row.ts if bar_row else None

        now = utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        first_snap_today = (
            await session.execute(
                sa.select(PositionSnapshot)
                .where(
                    PositionSnapshot.environment == environment,
                    PositionSnapshot.created_at >= day_start,
                )
                .order_by(PositionSnapshot.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        peak = (
            await session.execute(
                sa.select(sa.func.max(PositionSnapshot.equity)).where(
                    PositionSnapshot.environment == environment
                )
            )
        ).scalar_one_or_none()

        day_start_equity = first_snap_today.equity if first_snap_today else account.equity
        peak_equity = max(peak, account.equity) if peak is not None else account.equity

        return RiskContext(
            environment=environment,
            now=now,
            account=account,
            positions=positions,
            last_price=last_price,
            last_price_ts=last_price_ts,
            day_start_equity=day_start_equity,
            peak_equity=peak_equity,
        )

    # ------------------------------------------------------------ lifecycle sync

    async def sync_orders(self, session: AsyncSession) -> int:
        """Refresh non-terminal orders from the broker and ingest fills.
        Returns the number of orders updated."""
        environment = Environment(self.settings.trading_env)
        stmt = sa.select(Order).where(
            Order.environment == environment,
            Order.status.notin_(list(OrderStatus.terminal())),
        )
        open_orders = (await session.execute(stmt)).scalars().all()
        updated = 0
        for order in open_orders:
            state = await self.broker.get_order(order.client_order_id)
            if state is None:
                continue
            changed = (
                state.status != order.status or state.filled_qty != order.filled_qty
            )
            order.status = state.status
            order.filled_qty = state.filled_qty
            order.filled_avg_price = state.filled_avg_price
            order.broker_order_id = state.broker_order_id or order.broker_order_id
            if state.status in OrderStatus.terminal() and order.closed_at is None:
                order.closed_at = utcnow()
            if changed:
                updated += 1
        await session.flush()
        await self._ingest_fills(session, environment)
        return updated

    async def _ingest_fills(self, session: AsyncSession, environment: Environment) -> int:
        last_fill_at = (
            await session.execute(sa.select(sa.func.max(Fill.filled_at)))
        ).scalar_one_or_none()
        since = last_fill_at or (utcnow() - timedelta(days=7))
        fills = await self.broker.list_fills(since=since)
        created = 0
        for f in fills:
            order = None
            if f.client_order_id:
                order = (
                    await session.execute(
                        sa.select(Order).where(Order.client_order_id == f.client_order_id)
                    )
                ).scalar_one_or_none()
            if order is None and f.broker_order_id:
                order = (
                    await session.execute(
                        sa.select(Order).where(Order.broker_order_id == f.broker_order_id)
                    )
                ).scalar_one_or_none()
            if order is None:
                continue  # not ours (e.g. manual order in same account)
            exists = (
                await session.execute(
                    sa.select(Fill).where(
                        Fill.order_id == order.id, Fill.broker_fill_id == f.broker_fill_id
                    )
                )
            ).scalar_one_or_none()
            if exists:
                continue
            session.add(
                Fill(
                    order_id=order.id,
                    broker_fill_id=f.broker_fill_id,
                    qty=f.qty,
                    price=f.price,
                    filled_at=f.filled_at,
                )
            )
            created += 1
        await session.flush()
        return created

    async def snapshot_positions(self, session: AsyncSession) -> PositionSnapshot:
        account = await self.broker.get_account()
        positions = await self.broker.get_positions()
        snap = PositionSnapshot(
            environment=Environment(self.settings.trading_env),
            equity=account.equity,
            cash=account.cash,
            positions=[
                {
                    "symbol": p.symbol,
                    "qty": str(p.qty),
                    "avg_entry_price": str(p.avg_entry_price),
                    "market_value": str(p.market_value),
                    "unrealized_pl": str(p.unrealized_pl),
                }
                for p in positions
            ],
        )
        session.add(snap)
        await session.flush()
        return snap
