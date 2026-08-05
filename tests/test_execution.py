"""Tests for the execution service spine (app.execution.service).

A ``FakeBroker`` stands in for a real adapter: it records every submitted
``OrderRequest``, returns queued ``OrderState`` responses or raises queued
errors, and serves configurable account/position/order/fill state. The risk
engine runs with ``rules=[]`` so these tests exercise only the engine's
DB-backed halt checks (kill switch, duplicate detection) plus the execution
service's own invariants: audit-row-first, deterministic client_order_id,
shadow isolation, environment locking, and fill-ingestion idempotency.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.brokers.base import Broker, BrokerRejectionError, BrokerUnavailableError
from app.config import Settings
from app.db.base import utcnow
from app.db.models import (
    DecisionMode,
    DecisionStatus,
    Environment,
    Fill,
    MarketBar,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSnapshot,
    RiskEvent,
    RiskVerdict,
    SignalAction,
)
from app.execution.service import ExecutionService, client_order_id_for, size_qty
from app.risk import state as controls
from app.risk.engine import RiskEngine
from app.schemas.core import (
    AccountState,
    FillData,
    OrderRequest,
    OrderState,
    PositionState,
    TradeIntent,
)
from tests.conftest import make_account, make_position

SUBMITTED_AT = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)


class FakeBroker(Broker):
    """In-memory broker double.

    - ``submitted`` records every OrderRequest passed to ``submit_order``.
    - ``submit_queue`` holds OrderState responses or exceptions, consumed
      FIFO; when empty, a default ACCEPTED state echoing the request is
      returned.
    - ``order_states`` maps client_order_id -> OrderState for ``get_order``.
    - ``fills`` is returned verbatim from ``list_fills`` (the ``since``
      argument is deliberately ignored so the service's own dedupe logic is
      what gets exercised).
    """

    def __init__(
        self,
        environment: Environment = Environment.PAPER,
        account: AccountState | None = None,
    ) -> None:
        self.environment = environment
        self.account = account or make_account(env=environment)
        self.positions: list[PositionState] = []
        self.submitted: list[OrderRequest] = []
        self.submit_queue: list[OrderState | Exception] = []
        self.order_states: dict[str, OrderState] = {}
        self.fills: list[FillData] = []
        #: When set, get_account raises it — simulates a broker outage during
        #: risk-context construction.
        self.account_error: Exception | None = None

    async def get_account(self) -> AccountState:
        if self.account_error is not None:
            raise self.account_error
        return self.account

    async def get_positions(self) -> list[PositionState]:
        return list(self.positions)

    async def submit_order(self, request: OrderRequest) -> OrderState:
        self.submitted.append(request)
        if self.submit_queue:
            item = self.submit_queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return OrderState(
            client_order_id=request.client_order_id,
            broker_order_id=f"fake-{len(self.submitted)}",
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            order_type=request.order_type,
            status=OrderStatus.ACCEPTED,
            filled_qty=Decimal(0),
            filled_avg_price=None,
            submitted_at=SUBMITTED_AT,
            raw={"fake": True, "seq": len(self.submitted)},
        )

    async def get_order(self, client_order_id: str) -> OrderState | None:
        return self.order_states.get(client_order_id)

    async def cancel_order(self, client_order_id: str) -> None:
        return None

    async def list_open_orders(self) -> list[OrderState]:
        return [s for s in self.order_states.values() if s.status not in OrderStatus.terminal()]

    async def list_fills(self, since: datetime) -> list[FillData]:
        return list(self.fills)


# ---------------------------------------------------------------- helpers


def paper_settings() -> Settings:
    """Explicit paper settings; _env_file=None keeps local .env out of tests."""
    return Settings(
        _env_file=None,
        trading_env="paper",
        risk_duplicate_window_seconds=60,
        risk_circuit_breaker_errors=5,
        risk_circuit_breaker_window_seconds=300,
    )


def make_service(
    broker: FakeBroker | None = None, settings: Settings | None = None
) -> tuple[ExecutionService, FakeBroker]:
    settings = settings or paper_settings()
    broker = broker or FakeBroker(environment=Environment(settings.trading_env))
    engine = RiskEngine(settings, rules=[])
    return ExecutionService(broker, engine, settings), broker


async def seed_bar(session, symbol: str = "SPY", close: str = "100") -> MarketBar:
    """Recent MarketBar so risk context has a fresh last_price for the symbol."""
    px = Decimal(close)
    bar = MarketBar(
        symbol=symbol,
        timeframe="1Min",
        ts=utcnow(),
        open=px,
        high=px,
        low=px,
        close=px,
        volume=Decimal(1000),
    )
    session.add(bar)
    await session.flush()
    return bar


def buy_intent(
    symbol: str = "SPY", qty: str = "5", mode: DecisionMode = DecisionMode.PAPER
) -> TradeIntent:
    return TradeIntent(symbol=symbol, action=SignalAction.BUY, qty=Decimal(qty), mode=mode)


async def all_orders(session) -> list[Order]:
    return list((await session.execute(sa.select(Order))).scalars().all())


async def all_fills(session) -> list[Fill]:
    return list((await session.execute(sa.select(Fill))).scalars().all())


async def broker_error_events(session) -> list[RiskEvent]:
    stmt = sa.select(RiskEvent).where(RiskEvent.rule == "broker_error")
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------- execute_intent


async def test_happy_path_paper_buy(db_session):
    svc, broker = make_service()
    await seed_bar(db_session)

    decision = await svc.execute_intent(db_session, buy_intent())

    assert decision.risk_verdict == RiskVerdict.APPROVED
    assert decision.status == DecisionStatus.EXECUTED
    assert decision.mode == DecisionMode.PAPER
    assert decision.environment == Environment.PAPER

    orders = await all_orders(db_session)
    assert len(orders) == 1
    order = orders[0]
    assert order.decision_id == decision.id
    assert order.client_order_id == "qp-" + decision.id.hex
    assert order.client_order_id == client_order_id_for(decision.id)

    assert len(broker.submitted) == 1
    request = broker.submitted[0]
    assert request.client_order_id == order.client_order_id
    assert request.symbol == "SPY"
    assert request.side == OrderSide.BUY
    assert request.qty == Decimal("5")

    # Fields copied from the broker-returned OrderState.
    assert order.broker_order_id == "fake-1"
    assert order.status == OrderStatus.ACCEPTED
    assert order.filled_qty == Decimal(0)
    assert order.filled_avg_price is None
    # SQLite returns naive datetimes; compare in UTC terms.
    assert order.submitted_at.replace(tzinfo=UTC) == SUBMITTED_AT
    assert order.raw_broker_payload == {"fake": True, "seq": 1}


async def test_kill_switch_rejects_without_broker_call(db_session):
    svc, broker = make_service()
    await seed_bar(db_session)
    await controls.engage_kill_switch(
        db_session, actor="ops", reason="test halt", environment=Environment.PAPER
    )

    decision = await svc.execute_intent(db_session, buy_intent())

    assert decision.status == DecisionStatus.REJECTED
    assert decision.risk_verdict == RiskVerdict.HALTED
    verdicts = {r["rule"]: r["verdict"] for r in decision.risk_detail["results"]}
    assert verdicts["kill_switch"] == RiskVerdict.HALTED

    assert await all_orders(db_session) == []
    assert broker.submitted == []


async def test_shadow_mode_never_reaches_broker(db_session):
    svc, broker = make_service()
    await seed_bar(db_session, close="123.45")

    decision = await svc.execute_intent(db_session, buy_intent(mode=DecisionMode.SHADOW))

    assert decision.status == DecisionStatus.APPROVED
    assert decision.risk_verdict == RiskVerdict.APPROVED
    assert decision.outcome["shadow"] is True
    assert Decimal(decision.outcome["reference_price"]) == Decimal("123.45")

    assert await all_orders(db_session) == []
    assert broker.submitted == []


async def test_broker_rejection_marks_order_rejected(db_session):
    svc, broker = make_service()
    await seed_bar(db_session)
    broker.submit_queue.append(BrokerRejectionError("insufficient buying power"))

    decision = await svc.execute_intent(db_session, buy_intent())

    assert decision.status == DecisionStatus.FAILED
    orders = await all_orders(db_session)
    assert len(orders) == 1
    assert orders[0].status == OrderStatus.REJECTED
    assert "insufficient buying power" in orders[0].status_reason

    events = await broker_error_events(db_session)
    assert len(events) == 1
    assert events[0].detail["kind"] == "rejection"
    assert events[0].detail["client_order_id"] == orders[0].client_order_id


async def test_broker_unavailable_marks_order_error(db_session):
    svc, broker = make_service()
    await seed_bar(db_session)
    broker.submit_queue.append(BrokerUnavailableError("gateway timeout"))

    decision = await svc.execute_intent(db_session, buy_intent())

    assert decision.status == DecisionStatus.FAILED
    orders = await all_orders(db_session)
    assert len(orders) == 1
    assert orders[0].status == OrderStatus.ERROR
    assert "gateway timeout" in orders[0].status_reason

    events = await broker_error_events(db_session)
    assert len(events) == 1
    assert events[0].detail["kind"] == "unavailable"


async def test_duplicate_intent_rejected_end_to_end(db_session):
    svc, broker = make_service()
    await seed_bar(db_session)

    first = await svc.execute_intent(db_session, buy_intent())
    second = await svc.execute_intent(db_session, buy_intent())

    assert first.status == DecisionStatus.EXECUTED
    assert second.status == DecisionStatus.REJECTED
    assert second.risk_verdict == RiskVerdict.REJECTED
    rules = [r["rule"] for r in second.risk_detail["results"]]
    assert "duplicate_order" in rules

    assert len(await all_orders(db_session)) == 1
    assert len(broker.submitted) == 1


async def test_live_intent_downgraded_to_paper(db_session):
    svc, broker = make_service()
    await seed_bar(db_session)

    decision = await svc.execute_intent(db_session, buy_intent(mode=DecisionMode.LIVE))

    assert decision.mode == DecisionMode.PAPER
    assert decision.mode == "paper"
    assert decision.environment == Environment.PAPER
    assert decision.status == DecisionStatus.EXECUTED
    assert len(broker.submitted) == 1


def test_constructor_rejects_environment_mismatch():
    settings = paper_settings()
    broker = FakeBroker(environment=Environment.LIVE)
    with pytest.raises(RuntimeError, match="does not match"):
        ExecutionService(broker, RiskEngine(settings, rules=[]), settings)


# ---------------------------------------------------------------- lifecycle sync


async def test_sync_orders_updates_status_and_ingests_fills_once(db_session):
    svc, broker = make_service()
    coid = "qp-" + uuid.uuid4().hex
    order = Order(
        client_order_id=coid,
        environment=Environment.PAPER,
        symbol="SPY",
        side=OrderSide.BUY,
        qty=Decimal("5"),
        order_type=OrderType.MARKET,
        status=OrderStatus.SUBMITTED,
        filled_qty=Decimal(0),
    )
    db_session.add(order)
    await db_session.flush()

    broker.order_states[coid] = OrderState(
        client_order_id=coid,
        broker_order_id="bk-9",
        symbol="SPY",
        side=OrderSide.BUY,
        qty=Decimal("5"),
        order_type=OrderType.MARKET,
        status=OrderStatus.FILLED,
        filled_qty=Decimal("5"),
        filled_avg_price=Decimal("101.5"),
    )
    broker.fills = [
        FillData(
            broker_fill_id="f-1",
            client_order_id=coid,
            qty=Decimal("5"),
            price=Decimal("101.5"),
            filled_at=datetime(2026, 7, 17, 14, 30, tzinfo=UTC),
        )
    ]

    updated = await svc.sync_orders(db_session)

    assert updated == 1
    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == Decimal("5")
    assert order.filled_avg_price == Decimal("101.5")
    assert order.broker_order_id == "bk-9"
    assert order.closed_at is not None

    fills = await all_fills(db_session)
    assert len(fills) == 1
    assert fills[0].order_id == order.id
    assert fills[0].broker_fill_id == "f-1"
    assert fills[0].qty == Decimal("5")
    assert fills[0].price == Decimal("101.5")

    # Second sync: broker reports the same fill again; no duplicate row.
    await svc.sync_orders(db_session)
    assert len(await all_fills(db_session)) == 1


async def test_snapshot_positions_stringifies_fields(db_session):
    svc, broker = make_service()
    broker.positions = [make_position(symbol="SPY", qty="10", price="100")]

    snap = await svc.snapshot_positions(db_session)

    assert snap.environment == Environment.PAPER
    assert snap.equity == broker.account.equity
    assert snap.cash == broker.account.cash
    assert snap.positions == [
        {
            "symbol": "SPY",
            "qty": "10",
            "avg_entry_price": "100",
            "market_value": "1000",
            "unrealized_pl": "0",
        }
    ]

    rows = (await db_session.execute(sa.select(PositionSnapshot))).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == snap.id


# ---------------------------------------------------------------- sizing


def test_size_qty_floors_to_whole_shares():
    # cap = min(1000, 5% of 1_000_000 = 50_000) = 1000; 1000 / 333 = 3.003 -> 3
    assert size_qty(Decimal("1000000"), Decimal("333"), Decimal("1000")) == Decimal("3")


def test_size_qty_capped_by_max_notional():
    # cap = min(5000, 50_000) = 5000; 5000 / 100 -> 50 shares
    assert size_qty(Decimal("1000000"), Decimal("100"), Decimal("5000")) == Decimal("50")


def test_size_qty_capped_at_five_pct_of_equity():
    # 5% of 10_000 = 500 < max_notional 5000; 500 / 100 -> 5 shares
    assert size_qty(Decimal("10000"), Decimal("100"), Decimal("5000")) == Decimal("5")


def test_size_qty_zero_on_nonpositive_price():
    assert size_qty(Decimal("10000"), Decimal("0"), Decimal("5000")) == Decimal(0)
    assert size_qty(Decimal("10000"), Decimal("-1"), Decimal("5000")) == Decimal(0)


async def test_broker_failure_building_context_resolves_decision(db_session):
    """Regression: a broker error while building the risk context used to
    escape execute_intent, leaving an orphaned PROPOSED decision that nothing
    ever resolved or cleaned up."""
    svc, broker = make_service()
    broker.account_error = BrokerRejectionError("HTTP 401: unauthorized.")

    decision = await svc.execute_intent(db_session, buy_intent())

    assert decision.status == DecisionStatus.FAILED
    assert decision.risk_detail["error"] == "risk_context_unavailable"
    assert await all_orders(db_session) == []
    events = (
        await db_session.execute(
            sa.select(RiskEvent).where(RiskEvent.rule == "broker_error")
        )
    ).scalars().all()
    assert any(e.detail.get("kind") == "risk_context" for e in events)
