"""Manual order endpoints (spec-control-surface Phase A + B).

The governing property: a manual order is **not** a bypass. It builds an
ordinary TradeIntent and goes through the same ExecutionService, so every risk
rule applies and every order leaves an audit row. These tests pin that, plus
the corollary that a human's discretionary call never contaminates the
allocator's training data.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from app.api.deps import get_execution_service, require_admin
from app.api.routes import admin as admin_routes
from app.config import Settings, get_settings
from app.db.base import get_db_session, utcnow
from app.db.models import (
    Decision,
    DecisionMode,
    DecisionStatus,
    Environment,
    MarketBar,
    Order,
    OrderStatus,
    RiskEvent,
    RiskVerdict,
    Strategy,
    StrategyStatus,
)
from app.execution.service import ExecutionService
from app.risk.engine import RiskEngine
from tests.test_execution import FakeBroker

TOKEN = "secret"


def settings_for() -> Settings:
    return Settings(
        _env_file=None,
        admin_api_token=TOKEN,
        alpaca_paper_api_key="k",
        alpaca_paper_api_secret="s",
    )


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def app(session_factory, broker):
    application = FastAPI()
    application.include_router(admin_routes.router)

    async def override_session():
        async with session_factory() as session:
            yield session

    settings = settings_for()
    application.dependency_overrides[get_db_session] = override_session
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[require_admin] = lambda: None
    application.dependency_overrides[get_execution_service] = lambda: ExecutionService(
        broker, RiskEngine(settings, rules=[]), settings
    )
    return application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def seed_bar(session, symbol="SPY", close="500"):
    session.add(
        MarketBar(
            symbol=symbol, timeframe="1Day", ts=utcnow(),
            open=Decimal(close), high=Decimal(close), low=Decimal(close),
            close=Decimal(close), volume=Decimal(1000),
        )
    )
    await session.commit()


def order_body(**overrides) -> dict:
    body = {"symbol": "SPY", "action": "buy", "qty": 10,
            "actor": "ian", "reason": "manual entry"}
    body.update(overrides)
    return body


# ================================================================ Phase A


async def test_manual_order_places_and_records_origin(client, db_session, broker):
    await seed_bar(db_session)
    resp = await client.post("/admin/orders", json=order_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == DecisionStatus.EXECUTED
    assert body["qty"] == "10"
    assert body["sized_by"] == "operator"
    assert len(broker.submitted) == 1

    decision = (await db_session.execute(sa.select(Decision))).scalars().one()
    assert decision.context["origin"] == "manual"
    assert decision.context["actor"] == "ian"
    assert decision.context["manual_reason"] == "manual entry"
    # No strategy: this is the field that keeps it out of training.
    assert decision.strategy_id is None


async def test_manual_order_respects_the_kill_switch(client, db_session, broker):
    """The clearest proof it is not a bypass."""
    from app.risk import state as controls

    await seed_bar(db_session)
    await controls.engage_kill_switch(db_session, "ops", "halt", Environment.PAPER)
    await db_session.commit()

    resp = await client.post("/admin/orders", json=order_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == DecisionStatus.REJECTED
    assert body["risk_verdict"] == RiskVerdict.HALTED
    assert any("kill switch" in r for r in body["rejections"])
    assert broker.submitted == []  # never reached the broker


async def test_manual_order_rejected_by_a_risk_rule_reports_why(
    session_factory, broker, db_session
):
    """A manual order that violates a limit is rejected like any other, and the
    caller is told which rule and by how much."""
    await seed_bar(db_session, close="5000")

    application = FastAPI()
    application.include_router(admin_routes.router)
    settings = Settings(
        _env_file=None, admin_api_token=TOKEN,
        alpaca_paper_api_key="k", alpaca_paper_api_secret="s",
        risk_max_order_notional=1_000,  # 10 x 5000 will breach this
    )

    async def override_session():
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_db_session] = override_session
    application.dependency_overrides[get_settings] = lambda: settings
    application.dependency_overrides[require_admin] = lambda: None
    application.dependency_overrides[get_execution_service] = lambda: ExecutionService(
        broker, RiskEngine(settings), settings
    )

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/admin/orders", json=order_body())

    body = resp.json()
    assert body["status"] == DecisionStatus.REJECTED
    assert any("order notional" in r for r in body["rejections"])
    assert broker.submitted == []


async def test_manual_order_sizes_from_profile_when_qty_omitted(client, db_session):
    await seed_bar(db_session, close="500")
    resp = await client.post("/admin/orders", json=order_body(qty=None))
    body = resp.json()
    # Default profile: 25,000 / 5 positions = 5,000 budget / 500 = 10 shares.
    assert body["qty"] == "10"
    assert body["sized_by"] == "profile"


async def test_manual_order_without_price_requires_explicit_qty(client):
    resp = await client.post("/admin/orders", json=order_body(symbol="NOPE", qty=None))
    assert resp.status_code == 400
    assert "no price on record" in resp.json()["detail"]


async def test_manual_order_normalizes_symbol(client, db_session, broker):
    await seed_bar(db_session, symbol="SPY")
    resp = await client.post("/admin/orders", json=order_body(symbol=" spy "))
    assert resp.json()["symbol"] == "SPY"
    assert broker.submitted[0].symbol == "SPY"


@pytest.mark.parametrize(
    "body,field",
    [
        (order_body(actor=""), "actor"),
        (order_body(reason=""), "reason"),
        (order_body(qty=0), "qty"),
        (order_body(qty=-5), "qty"),
        (order_body(action="short"), "action"),
    ],
)
async def test_manual_order_validation(client, body, field):
    """Actor and reason are mandatory: an unattributable manual trade is
    exactly what the audit spine exists to prevent."""
    resp = await client.post("/admin/orders", json=body)
    assert resp.status_code == 422


async def test_manual_order_without_broker_is_refused(session_factory, db_session):
    """Better a clear 503 than silently doing nothing."""
    application = FastAPI()
    application.include_router(admin_routes.router)

    async def override_session():
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_db_session] = override_session
    application.dependency_overrides[get_settings] = settings_for
    application.dependency_overrides[require_admin] = lambda: None
    application.dependency_overrides[get_execution_service] = lambda: None

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/admin/orders", json=order_body())
    assert resp.status_code == 503
    assert "no broker configured" in resp.json()["detail"]


# ---------------------------------------------------------------- cancel


async def seed_order(session, status=OrderStatus.ACCEPTED, cid="qp-live") -> Order:
    order = Order(
        client_order_id=cid, environment=Environment.PAPER, symbol="SPY",
        side="buy", qty=Decimal(10), order_type="market", status=status,
    )
    session.add(order)
    await session.commit()
    return order


async def test_cancel_withdraws_a_working_order(client, db_session, broker):
    await seed_order(db_session)
    resp = await client.post(
        "/admin/orders/qp-live/cancel",
        json={"actor": "ian", "reason": "changed my mind"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == OrderStatus.CANCELED

    order = (
        await db_session.execute(sa.select(Order).where(Order.client_order_id == "qp-live"))
    ).scalars().one()
    await db_session.refresh(order)
    assert order.status == OrderStatus.CANCELED
    assert "ian" in order.status_reason
    assert order.closed_at is not None

    event = (
        await db_session.execute(
            sa.select(RiskEvent).where(RiskEvent.rule == "manual_cancel")
        )
    ).scalars().one()
    assert event.detail["actor"] == "ian"


async def test_cancel_is_idempotent_on_terminal_orders(client, db_session):
    await seed_order(db_session, status=OrderStatus.FILLED, cid="qp-done")
    resp = await client.post(
        "/admin/orders/qp-done/cancel", json={"actor": "ian", "reason": "oops"}
    )
    assert resp.status_code == 200
    assert "nothing to cancel" in resp.json()["note"]


async def test_cancel_unknown_order_is_404(client):
    resp = await client.post(
        "/admin/orders/qp-nope/cancel", json={"actor": "ian", "reason": "x"}
    )
    assert resp.status_code == 404


# ================================================================ Phase B


async def test_manual_decisions_never_reach_allocator_training(db_session):
    """A human's discretionary call must not be attributed to whatever model
    happened to be champion. `strategy_id IS NULL` is what enforces that."""
    from app.learning.training import train_allocator

    strategy = Strategy(
        name="real_strategy", version=1, class_path="x.Y", params={},
        status=StrategyStatus.APPROVED,
    )
    db_session.add(strategy)
    await db_session.flush()

    now = utcnow()
    # One manual decision and one strategy decision, both closed with outcomes.
    db_session.add(
        Decision(
            mode=DecisionMode.PAPER, environment=Environment.PAPER,
            status=DecisionStatus.CLOSED, strategy_id=None, symbol="SPY",
            action="buy", qty=Decimal(1),
            context={"origin": "manual", "regime": "trend_up"},
            outcome={"return_pct": "0.99"},  # wildly good, would skew training
            created_at=now - timedelta(days=1),
        )
    )
    db_session.add(
        Decision(
            mode=DecisionMode.PAPER, environment=Environment.PAPER,
            status=DecisionStatus.CLOSED, strategy_id=strategy.id, symbol="SPY",
            action="buy", qty=Decimal(1),
            context={"regime": "trend_up"},
            outcome={"return_pct": "0.01"},
            created_at=now - timedelta(days=1),
        )
    )
    await db_session.flush()

    model = await train_allocator(db_session, now=now)
    # Only the strategy decision was used.
    assert model.training_meta["n_decisions"] == 1
    assert model.artifact["scores"]["trend_up"]["real_strategy"] == pytest.approx(
        0.01, abs=1e-6
    )


async def test_manual_decisions_excluded_from_performance_reports(db_session):
    from app.learning.evaluation import build_performance_reports

    now = utcnow()
    db_session.add(
        Decision(
            mode=DecisionMode.PAPER, environment=Environment.PAPER,
            status=DecisionStatus.CLOSED, strategy_id=None, symbol="SPY",
            action="buy", qty=Decimal(1),
            context={"origin": "manual", "regime": "trend_up"},
            outcome={"return_pct": "0.5", "realized_pnl": "100"},
            created_at=now - timedelta(hours=1),
        )
    )
    await db_session.flush()

    reports = await build_performance_reports(
        db_session, now - timedelta(days=1), now + timedelta(days=1)
    )
    assert reports == []
