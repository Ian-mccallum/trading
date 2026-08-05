"""Learning subsystem tests: features/regimes, registry promotion gates,
allocator ranking, outcome evaluation, champion/challenger, training."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.db.base import utcnow
from app.db.models import (
    Decision,
    DecisionMode,
    DecisionStatus,
    Environment,
    Fill,
    MarketBar,
    ModelStatus,
    Order,
    OrderStatus,
    PromotionEvent,
    Strategy,
    StrategyStatus,
)
from app.learning.allocator import (
    ARTIFACT_TYPE,
    StrategyAllocator,
    load_champion_allocator,
)
from app.learning.evaluation import (
    build_performance_reports,
    compare_champion_challenger,
    evaluate_decision_outcomes,
)
from app.learning.features import classify_regime, compute_features, market_context
from app.learning.registry import ModelRegistry
from app.learning.training import train_allocator
from tests.conftest import make_bars

# ---------------------------------------------------------------- features


def test_features_uptrend_regime():
    bars = make_bars(n=80, start_price=100, step=1.0)
    features = compute_features(bars)
    assert features["sma_ratio_20_50"] > 0.01
    assert features["realized_vol_20"] is not None
    assert 0 <= features["rsi_14"] <= 100
    assert classify_regime(features) == "trend_up"


def test_features_downtrend_regime():
    bars = make_bars(n=80, start_price=200, step=-1.0)
    assert classify_regime(compute_features(bars)) == "trend_down"


def test_features_flat_series_low_vol_range():
    bars = make_bars(n=80, start_price=100, step=0.0)
    features = compute_features(bars)
    assert features["rsi_14"] == 50.0  # flat series is neutral by convention
    assert classify_regime(features) == "low_vol_range"


def test_features_short_history():
    assert compute_features(make_bars(n=1)) == {}
    ctx = market_context(make_bars(n=5))
    assert ctx["regime"] == "unknown"  # not enough bars for sma50/vol


# ---------------------------------------------------------------- registry


async def test_register_increments_version(db_session):
    registry = ModelRegistry()
    m1 = await registry.register(db_session, "alloc", {"type": ARTIFACT_TYPE}, {}, {})
    m2 = await registry.register(db_session, "alloc", {"type": ARTIFACT_TYPE}, {}, {})
    assert (m1.version, m2.version) == (1, 2)
    assert m1.status == ModelStatus.REGISTERED


async def test_promotion_transitions(db_session):
    registry = ModelRegistry()
    model = await registry.register(db_session, "alloc", {}, {}, {})

    with pytest.raises(ValueError, match="illegal transition"):
        await registry.promote(
            db_session, model.id, ModelStatus.CHAMPION, actor="ops", reason="skip shadow"
        )
    with pytest.raises(ValueError, match="actor"):
        await registry.promote(db_session, model.id, ModelStatus.SHADOW, actor="  ", reason="x")

    await registry.promote(db_session, model.id, ModelStatus.SHADOW, actor="ops", reason="test")
    assert model.status == ModelStatus.SHADOW
    await registry.promote(db_session, model.id, ModelStatus.CHAMPION, actor="ops", reason="ok")
    assert model.status == ModelStatus.CHAMPION

    with pytest.raises(ValueError, match="not found"):
        await registry.promote(
            db_session, uuid.uuid4(), ModelStatus.SHADOW, actor="ops", reason="x"
        )


async def test_champion_promotion_demotes_previous(db_session):
    registry = ModelRegistry()
    old = await registry.register(db_session, "alloc", {}, {}, {})
    new = await registry.register(db_session, "alloc", {}, {}, {})
    for m in (old, new):
        await registry.promote(db_session, m.id, ModelStatus.SHADOW, actor="ops", reason="t")
    await registry.promote(db_session, old.id, ModelStatus.CHAMPION, actor="ops", reason="t")
    await registry.promote(db_session, new.id, ModelStatus.CHAMPION, actor="ops", reason="t")

    assert old.status == ModelStatus.RETIRED
    assert new.status == ModelStatus.CHAMPION
    events = (await db_session.execute(sa.select(PromotionEvent))).scalars().all()
    # 2x ->shadow, old->champion, old->retired (displaced), new->champion
    assert len(events) == 5
    assert all(e.actor == "ops" for e in events)


# ---------------------------------------------------------------- allocator


def strategy_row(name: str, status: str = StrategyStatus.APPROVED) -> Strategy:
    return Strategy(
        id=uuid.uuid4(), name=name, version=1,
        class_path=f"app.strategies.builtin.{name}.X", params={}, status=status,
    )


def test_allocator_ranks_by_regime_scores():
    sma, rsi = strategy_row("sma_cross"), strategy_row("rsi_reversion")
    artifact = {
        "type": ARTIFACT_TYPE,
        "scores": {"trend_up": {"sma_cross": 0.9, "rsi_reversion": 0.1}},
        "default": {"sma_cross": 0.2, "rsi_reversion": 0.5},
    }
    model = type("M", (), {"artifact": artifact})()
    allocator = StrategyAllocator(model)

    assert allocator.select("trend_up", [rsi, sma]).name == "sma_cross"
    # Unknown regime falls back to defaults.
    assert allocator.select("low_vol_range", [rsi, sma]).name == "rsi_reversion"


def test_allocator_filters_unapproved_and_handles_empty():
    candidate = strategy_row("sma_cross", status=StrategyStatus.CANDIDATE)
    retired = strategy_row("rsi_reversion", status=StrategyStatus.RETIRED)
    allocator = StrategyAllocator(None)
    assert allocator.select("trend_up", [candidate, retired]) is None
    assert allocator.select("trend_up", []) is None

    approved = strategy_row("sma_cross")
    assert allocator.select("trend_up", [candidate, approved]).name == "sma_cross"


async def test_load_champion_allocator_fallback(db_session):
    allocator = await load_champion_allocator(db_session)
    assert allocator.model_version is None  # uniform fallback, still usable
    approved = strategy_row("sma_cross")
    assert allocator.select("unknown", [approved]).name == "sma_cross"


# ---------------------------------------------------------------- evaluation


async def seed_strategy(session, name="sma_cross") -> Strategy:
    row = Strategy(
        name=name, version=1, class_path=f"x.{name}", params={},
        status=StrategyStatus.APPROVED,
    )
    session.add(row)
    await session.flush()
    return row


async def seed_bar(session, symbol="SPY", close="110", ts=None):
    session.add(
        MarketBar(
            symbol=symbol, timeframe="1Day", ts=ts or utcnow(),
            open=Decimal(close), high=Decimal(close), low=Decimal(close),
            close=Decimal(close), volume=Decimal(1000),
        )
    )
    await session.flush()


async def seed_executed_decision(
    session, strategy, *, qty="10", fill_price="100", action="buy",
    mode=DecisionMode.PAPER, model_version_id=None, regime="trend_up",
) -> Decision:
    decision = Decision(
        mode=mode, environment=Environment.PAPER, status=DecisionStatus.EXECUTED,
        strategy_id=strategy.id, model_version_id=model_version_id,
        symbol="SPY", action=action, qty=Decimal(qty),
        context={"regime": regime, "features": {}},
    )
    session.add(decision)
    await session.flush()
    order = Order(
        decision_id=decision.id, client_order_id=f"qp-{decision.id.hex}",
        environment=Environment.PAPER, symbol="SPY", side="buy",
        qty=Decimal(qty), order_type="market", status=OrderStatus.FILLED,
    )
    session.add(order)
    await session.flush()
    session.add(
        Fill(
            order_id=order.id, broker_fill_id=f"f-{decision.id.hex[:8]}",
            qty=Decimal(qty), price=Decimal(fill_price), filled_at=utcnow(),
        )
    )
    await session.flush()
    return decision


async def test_evaluate_executed_decision(db_session):
    strategy = await seed_strategy(db_session)
    decision = await seed_executed_decision(db_session, strategy, qty="10", fill_price="100")
    await seed_bar(db_session, close="110")

    later = utcnow() + timedelta(hours=2)
    closed = await evaluate_decision_outcomes(db_session, now=later, horizon_minutes=60)

    assert closed == 1
    assert decision.status == DecisionStatus.CLOSED
    assert Decimal(decision.outcome["realized_pnl"]) == Decimal(100)  # (110-100)*10
    assert Decimal(decision.outcome["return_pct"]) == Decimal("0.1")
    assert decision.outcome_recorded_at is not None


async def test_evaluate_skips_recent_and_unfilled(db_session):
    strategy = await seed_strategy(db_session)
    # Unfilled decision: EXECUTED but no fills.
    unfilled = Decision(
        mode=DecisionMode.PAPER, environment=Environment.PAPER,
        status=DecisionStatus.EXECUTED, strategy_id=strategy.id,
        symbol="SPY", action="buy", qty=Decimal(1), context={},
    )
    db_session.add(unfilled)
    await seed_bar(db_session)
    await db_session.flush()

    closed = await evaluate_decision_outcomes(
        db_session, now=utcnow() + timedelta(hours=2)
    )
    assert closed == 0
    assert unfilled.status == DecisionStatus.EXECUTED  # untouched, retried later


async def test_evaluate_shadow_decision(db_session):
    strategy = await seed_strategy(db_session)
    shadow = Decision(
        mode=DecisionMode.SHADOW, environment=Environment.PAPER,
        status=DecisionStatus.APPROVED, strategy_id=strategy.id,
        symbol="SPY", action="buy", qty=Decimal(5),
        context={"regime": "trend_up"},
        outcome={"shadow": True, "reference_price": "100"},
    )
    db_session.add(shadow)
    await seed_bar(db_session, close="90")
    await db_session.flush()

    closed = await evaluate_decision_outcomes(db_session, now=utcnow() + timedelta(hours=2))
    assert closed == 1
    assert shadow.status == DecisionStatus.CLOSED
    assert Decimal(shadow.outcome["realized_pnl"]) == Decimal(-50)  # (90-100)*5
    assert shadow.outcome["shadow"] is True


async def test_build_performance_reports_groups(db_session):
    strategy = await seed_strategy(db_session)
    await seed_bar(db_session, close="110")
    for _ in range(3):
        await seed_executed_decision(db_session, strategy, regime="trend_up")
    await evaluate_decision_outcomes(db_session, now=utcnow() + timedelta(hours=2))

    start = utcnow() - timedelta(days=1)
    end = utcnow() + timedelta(days=1)
    reports = await build_performance_reports(db_session, start, end)
    assert len(reports) == 1
    report = reports[0]
    assert report.regime == "trend_up"
    assert report.metrics["n"] == 3
    assert report.metrics["win_rate"] == 1.0


async def test_champion_challenger_comparison(db_session):
    registry = ModelRegistry()
    strategy = await seed_strategy(db_session)
    champion = await registry.register(db_session, "alloc", {}, {}, {})
    challenger = await registry.register(db_session, "alloc", {}, {}, {})
    start, end = utcnow() - timedelta(days=1), utcnow() + timedelta(days=1)

    # Insufficient data path.
    experiment = await compare_champion_challenger(
        db_session, "cc-1", champion, challenger, start, end
    )
    assert experiment.results["recommendation"] == "insufficient_data"

    # Champion loses money, challenger profits; >= 10 closed decisions per side.
    await seed_bar(db_session, close="110")
    for model, price in ((champion, "120"), (challenger, "100")):
        for _ in range(10):
            await seed_executed_decision(
                db_session, strategy, fill_price=price, model_version_id=model.id
            )
    await evaluate_decision_outcomes(db_session, now=utcnow() + timedelta(hours=2))

    experiment = await compare_champion_challenger(
        db_session, "cc-2", champion, challenger, start, end + timedelta(hours=3)
    )
    assert experiment.results["recommendation"] == "promote_challenger"
    assert experiment.results["challenger"]["n"] == 10
    # Statuses are untouched — recommendation only.
    assert champion.status == ModelStatus.REGISTERED
    assert challenger.status == ModelStatus.REGISTERED


# ---------------------------------------------------------------- training


async def test_train_allocator_from_history(db_session):
    sma = await seed_strategy(db_session, "sma_cross")
    rsi = await seed_strategy(db_session, "rsi_reversion")
    await seed_bar(db_session, close="110")
    # sma_cross wins in trend_up (bought at 100 -> 110); rsi loses (bought at 120).
    for _ in range(5):
        await seed_executed_decision(db_session, sma, fill_price="100", regime="trend_up")
        await seed_executed_decision(db_session, rsi, fill_price="120", regime="trend_up")
    now = utcnow() + timedelta(hours=2)
    await evaluate_decision_outcomes(db_session, now=now)

    model = await train_allocator(db_session, now=now)
    artifact = model.artifact
    assert artifact["type"] == ARTIFACT_TYPE
    assert artifact["scores"]["trend_up"]["sma_cross"] > artifact["scores"]["trend_up"]["rsi_reversion"]
    assert model.status == ModelStatus.REGISTERED  # inert until humanly promoted
    assert model.training_meta["n_decisions"] == 10

    # The trained artifact actually drives selection.
    allocator = StrategyAllocator(model)
    assert allocator.select("trend_up", [sma, rsi]).name == "sma_cross"


async def test_train_allocator_refuses_empty_history(db_session):
    with pytest.raises(ValueError, match="no closed decisions"):
        await train_allocator(db_session, now=utcnow())
