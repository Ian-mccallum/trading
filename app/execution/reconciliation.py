"""Reconciliation between our order records and the broker's.

Two jobs:

1. Resolve ERROR-status orders (submission outcome unknown): ask the broker
   for the client_order_id; adopt its state if it landed, or mark REJECTED
   after a grace period if it never did.
2. Detect broker open orders we have no record of (manual orders or a bug) —
   logged and recorded as a RiskEvent so an operator sees them.
"""

from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.base import Broker, BrokerError
from app.db.base import utcnow
from app.db.models import Environment, Order, OrderStatus, RiskEvent, RiskVerdict
from app.logging import get_logger

log = get_logger("execution.reconciliation")

UNRESOLVED_GRACE = timedelta(minutes=10)


async def reconcile(session: AsyncSession, broker: Broker) -> dict:
    resolved = await _resolve_error_orders(session, broker)
    unknown = await _detect_unknown_broker_orders(session, broker)
    return {"resolved_error_orders": resolved, "unknown_broker_orders": unknown}


async def _resolve_error_orders(session: AsyncSession, broker: Broker) -> int:
    rows = (
        (
            await session.execute(
                sa.select(Order).where(
                    Order.environment == broker.environment,
                    Order.status == OrderStatus.ERROR,
                )
            )
        )
        .scalars()
        .all()
    )
    resolved = 0
    for order in rows:
        try:
            state = await broker.get_order(order.client_order_id)
        except BrokerError as exc:
            log.warning("reconcile_lookup_failed", client_order_id=order.client_order_id,
                        error=str(exc))
            continue
        if state is not None:
            order.status = state.status
            order.broker_order_id = state.broker_order_id or order.broker_order_id
            order.filled_qty = state.filled_qty
            order.filled_avg_price = state.filled_avg_price
            order.status_reason = "resolved by reconciliation"
            resolved += 1
        elif utcnow() - order.created_at > UNRESOLVED_GRACE:
            order.status = OrderStatus.REJECTED
            order.status_reason = "never reached broker (reconciliation timeout)"
            order.closed_at = utcnow()
            resolved += 1
    await session.flush()
    return resolved


async def _detect_unknown_broker_orders(session: AsyncSession, broker: Broker) -> int:
    try:
        open_orders = await broker.list_open_orders()
    except BrokerError as exc:
        log.warning("reconcile_list_failed", error=str(exc))
        return 0
    unknown = 0
    for state in open_orders:
        exists = (
            await session.execute(
                sa.select(Order.id).where(Order.client_order_id == state.client_order_id)
            )
        ).scalar_one_or_none()
        if exists is None:
            unknown += 1
            log.warning(
                "unknown_broker_order",
                client_order_id=state.client_order_id,
                symbol=state.symbol,
            )
            session.add(
                RiskEvent(
                    rule="unknown_order",
                    verdict=RiskVerdict.REJECTED,
                    severity="warning",
                    environment=Environment(broker.environment),
                    detail={
                        "client_order_id": state.client_order_id,
                        "symbol": state.symbol,
                        "note": "open order at broker with no local record",
                    },
                )
            )
    await session.flush()
    return unknown
