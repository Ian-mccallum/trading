"""Tests for the pure risk rules (app.risk.rules) and the risk engine
orchestration (app.risk.engine).

Rule unit tests hand-build ``RiskContext`` objects and never touch the DB.
Engine tests run against the in-memory SQLite fixture and exercise the
DB-backed halt checks (kill switch, circuit breaker, live gate, duplicate
detection) plus RiskEvent persistence.

All Settings are constructed with ``_env_file=None`` so a local .env can
never leak into test behavior.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from app.config import LIVE_CONFIRMATION_PHRASE, Settings
from app.db.base import utcnow
from app.db.models import (
    Environment,
    Order,
    OrderStatus,
    OrderType,
    RiskEvent,
    RiskVerdict,
    SignalAction,
)
from app.risk import state as controls
from app.risk.engine import RiskEngine
from app.risk.rules import (
    DailyLossRule,
    DrawdownRule,
    GrossExposureRule,
    NoShortSellRule,
    OrderNotionalRule,
    PositionLimitRule,
    QtySanityRule,
    StaleDataRule,
    SymbolAllowlistRule,
    default_rules,
)
from app.risk.types import RiskContext
from app.schemas.core import PositionState, TradeIntent
from tests.conftest import make_account, make_position

NOW = datetime(2026, 7, 17, 15, 30, tzinfo=UTC)

EXPECTED_RULE_ORDER = [
    "qty_sanity",
    "symbol_allowlist",
    "stale_data",
    "order_notional",
    "position_limit",
    "gross_exposure",
    "daily_loss",
    "drawdown",
    "no_short_sell",
]


# ---------------------------------------------------------------- helpers


def make_settings(**overrides) -> Settings:
    defaults: dict = {
        "risk_max_order_notional": 5_000.0,
        "risk_max_position_notional": 10_000.0,
        "risk_max_gross_exposure": 25_000.0,
        "risk_max_daily_loss": 500.0,
        "risk_max_drawdown_pct": 10.0,
        "risk_duplicate_window_seconds": 60,
        "risk_max_data_age_seconds": 300,
        "risk_circuit_breaker_errors": 5,
        "risk_circuit_breaker_window_seconds": 300,
        "risk_symbol_allowlist": "",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def make_ctx(
    *,
    now: datetime = NOW,
    env: Environment = Environment.PAPER,
    equity: str = "100000",
    positions: list[PositionState] | None = None,
    last_price: str | None = "100",
    price_age_seconds: int = 10,
    day_start_equity: str | None = "100000",
    peak_equity: str | None = "100000",
) -> RiskContext:
    price = Decimal(last_price) if last_price is not None else None
    return RiskContext(
        environment=env,
        now=now,
        account=make_account(equity, env=env),
        positions=list(positions or []),
        last_price=price,
        last_price_ts=now - timedelta(seconds=price_age_seconds) if price is not None else None,
        day_start_equity=Decimal(day_start_equity) if day_start_equity is not None else None,
        peak_equity=Decimal(peak_equity) if peak_equity is not None else None,
    )


def intent(
    action: SignalAction = SignalAction.BUY,
    symbol: str = "SPY",
    qty: str = "10",
    limit: str | None = None,
) -> TradeIntent:
    return TradeIntent(
        symbol=symbol,
        action=action,
        qty=Decimal(qty),
        order_type=OrderType.LIMIT if limit is not None else OrderType.MARKET,
        limit_price=Decimal(limit) if limit is not None else None,
    )


def buy(**kw) -> TradeIntent:
    return intent(SignalAction.BUY, **kw)


def sell(**kw) -> TradeIntent:
    return intent(SignalAction.SELL, **kw)


def close(symbol: str = "SPY") -> TradeIntent:
    # qty is a placeholder: rules treat close as "sell the whole long position".
    return intent(SignalAction.CLOSE, symbol=symbol, qty="1")


# ================================================================ rule units


class TestQtySanityRule:
    def rule(self) -> QtySanityRule:
        return QtySanityRule(make_settings())

    def test_normal_qty_approved(self):
        result = self.rule().check(buy(qty="10"), make_ctx())
        assert result.approved
        assert result.rule == "qty_sanity"

    def test_qty_over_hard_cap_rejected(self):
        result = self.rule().check(buy(qty="10001"), make_ctx())
        assert result.verdict == RiskVerdict.REJECTED
        assert "10001" in result.reason

    def test_qty_at_cap_approved(self):
        assert self.rule().check(buy(qty="10000"), make_ctx()).approved

    def test_non_finite_qty_rejected(self):
        bad = TradeIntent.model_construct(
            symbol="SPY",
            action=SignalAction.BUY,
            qty=Decimal("NaN"),
            order_type=OrderType.MARKET,
            limit_price=None,
        )
        result = self.rule().check(bad, make_ctx())
        assert result.verdict == RiskVerdict.REJECTED
        assert "finite" in result.reason

    def test_zero_qty_rejected(self):
        bad = TradeIntent.model_construct(
            symbol="SPY",
            action=SignalAction.SELL,
            qty=Decimal(0),
            order_type=OrderType.MARKET,
            limit_price=None,
        )
        assert self.rule().check(bad, make_ctx()).verdict == RiskVerdict.REJECTED

    def test_close_while_flat_rejected(self):
        """Close with no position sizes to 0 shares and must be rejected."""
        result = self.rule().check(close(), make_ctx(positions=[]))
        assert result.verdict == RiskVerdict.REJECTED
        assert "no long position" in result.reason

    def test_close_with_long_position_approved(self):
        ctx = make_ctx(positions=[make_position("SPY", qty="25")])
        assert self.rule().check(close(), ctx).approved


class TestSymbolAllowlistRule:
    def test_empty_allowlist_allows_anything(self):
        rule = SymbolAllowlistRule(make_settings(risk_symbol_allowlist=""))
        assert rule.check(buy(symbol="TSLA"), make_ctx()).approved

    def test_listed_symbol_approved(self):
        rule = SymbolAllowlistRule(make_settings(risk_symbol_allowlist="SPY,QQQ"))
        assert rule.check(buy(symbol="SPY"), make_ctx()).approved

    def test_case_insensitive_match(self):
        rule = SymbolAllowlistRule(make_settings(risk_symbol_allowlist="spy"))
        assert rule.check(buy(symbol="SPY"), make_ctx()).approved

    def test_unlisted_symbol_rejected(self):
        rule = SymbolAllowlistRule(make_settings(risk_symbol_allowlist="SPY,QQQ"))
        result = rule.check(buy(symbol="TSLA"), make_ctx())
        assert result.verdict == RiskVerdict.REJECTED
        assert "TSLA" in result.reason


class TestStaleDataRule:
    def rule(self) -> StaleDataRule:
        return StaleDataRule(make_settings(risk_max_data_age_seconds=300))

    def test_fresh_price_approved(self):
        assert self.rule().check(buy(), make_ctx(price_age_seconds=10)).approved

    def test_old_price_rejected(self):
        result = self.rule().check(buy(), make_ctx(price_age_seconds=301))
        assert result.verdict == RiskVerdict.REJECTED

    def test_age_exactly_at_limit_rejected(self):
        """Price must be strictly newer than the cutoff."""
        result = self.rule().check(buy(), make_ctx(price_age_seconds=300))
        assert result.verdict == RiskVerdict.REJECTED

    def test_missing_price_fails_closed(self):
        result = self.rule().check(buy(), make_ctx(last_price=None))
        assert result.verdict == RiskVerdict.REJECTED
        assert "no market price" in result.reason

    def test_missing_timestamp_fails_closed(self):
        ctx = dataclasses.replace(make_ctx(), last_price_ts=None)
        assert self.rule().check(buy(), ctx).verdict == RiskVerdict.REJECTED


class TestOrderNotionalRule:
    def rule(self) -> OrderNotionalRule:
        return OrderNotionalRule(make_settings(risk_max_order_notional=5_000.0))

    def test_small_order_approved(self):
        assert self.rule().check(buy(qty="10"), make_ctx(last_price="100")).approved

    def test_notional_at_limit_approved(self):
        assert self.rule().check(buy(qty="50"), make_ctx(last_price="100")).approved

    def test_oversized_order_rejected(self):
        result = self.rule().check(buy(qty="60"), make_ctx(last_price="100"))
        assert result.verdict == RiskVerdict.REJECTED
        assert result.detail["notional"] == "6000"

    def test_limit_price_takes_precedence(self):
        # Market price would give 10 * 100 = 1000; the limit price makes it 6000.
        result = self.rule().check(buy(qty="10", limit="600"), make_ctx(last_price="100"))
        assert result.verdict == RiskVerdict.REJECTED

    def test_unpriceable_order_fails_closed(self):
        result = self.rule().check(buy(qty="10"), make_ctx(last_price=None))
        assert result.verdict == RiskVerdict.REJECTED


class TestPositionLimitRule:
    def rule(self) -> PositionLimitRule:
        return PositionLimitRule(make_settings(risk_max_position_notional=10_000.0))

    def test_buy_within_limit_approved(self):
        assert self.rule().check(buy(qty="50"), make_ctx(last_price="100")).approved

    def test_buy_pushing_position_over_limit_rejected(self):
        ctx = make_ctx(positions=[make_position("SPY", qty="60", price="100")], last_price="100")
        result = self.rule().check(buy(qty="50"), ctx)
        assert result.verdict == RiskVerdict.REJECTED
        assert result.detail["projected_notional"] == "11000"

    def test_risk_reducing_sell_approved_even_over_limit(self):
        # Position already exceeds the cap; trimming it must still be allowed.
        ctx = make_ctx(positions=[make_position("SPY", qty="200", price="100")], last_price="100")
        result = self.rule().check(sell(qty="50"), ctx)
        assert result.approved
        assert "risk-reducing" in result.reason

    def test_close_of_oversized_position_approved(self):
        ctx = make_ctx(positions=[make_position("SPY", qty="200", price="100")], last_price="100")
        assert self.rule().check(close(), ctx).approved

    def test_unpriceable_increase_fails_closed(self):
        result = self.rule().check(buy(qty="10"), make_ctx(last_price=None))
        assert result.verdict == RiskVerdict.REJECTED


class TestGrossExposureRule:
    def rule(self) -> GrossExposureRule:
        return GrossExposureRule(make_settings(risk_max_gross_exposure=25_000.0))

    def test_buy_within_exposure_approved(self):
        positions = [
            make_position("SPY", qty="100", price="100"),  # 10_000
            make_position("QQQ", qty="140", price="100"),  # 14_000
        ]
        ctx = make_ctx(positions=positions, last_price="100")
        assert self.rule().check(buy(symbol="MSFT", qty="5"), ctx).approved

    def test_buy_exceeding_exposure_rejected(self):
        positions = [
            make_position("SPY", qty="100", price="100"),
            make_position("QQQ", qty="140", price="100"),
        ]
        ctx = make_ctx(positions=positions, last_price="100")
        result = self.rule().check(buy(symbol="MSFT", qty="20"), ctx)
        assert result.verdict == RiskVerdict.REJECTED
        assert result.detail["projected_exposure"] == "26000"

    def test_risk_reducing_sell_always_approved(self):
        # Gross exposure already above the cap; reducing must pass.
        ctx = make_ctx(positions=[make_position("SPY", qty="300", price="100")], last_price="100")
        result = self.rule().check(sell(qty="50"), ctx)
        assert result.approved
        assert "risk-reducing" in result.reason


class TestDailyLossRule:
    def rule(self) -> DailyLossRule:
        return DailyLossRule(make_settings(risk_max_daily_loss=500.0))

    def test_small_loss_approved(self):
        ctx = make_ctx(equity="99600", day_start_equity="100000")
        assert self.rule().check(buy(), ctx).approved

    def test_loss_at_limit_rejected(self):
        ctx = make_ctx(equity="99500", day_start_equity="100000")
        result = self.rule().check(buy(), ctx)
        assert result.verdict == RiskVerdict.REJECTED
        assert "daily loss" in result.reason

    def test_unknown_day_start_approves(self):
        ctx = make_ctx(equity="1", day_start_equity=None)
        assert self.rule().check(buy(), ctx).approved


class TestDrawdownRule:
    def rule(self) -> DrawdownRule:
        return DrawdownRule(make_settings(risk_max_drawdown_pct=10.0))

    def test_small_drawdown_approved(self):
        ctx = make_ctx(equity="95000", peak_equity="100000")
        assert self.rule().check(buy(), ctx).approved

    def test_drawdown_at_limit_rejected(self):
        ctx = make_ctx(equity="90000", peak_equity="100000")
        result = self.rule().check(buy(), ctx)
        assert result.verdict == RiskVerdict.REJECTED
        assert "drawdown" in result.reason

    def test_unknown_peak_approves(self):
        ctx = make_ctx(equity="1", peak_equity=None)
        assert self.rule().check(buy(), ctx).approved


class TestNoShortSellRule:
    def rule(self) -> NoShortSellRule:
        return NoShortSellRule(make_settings())

    def test_buy_always_approved(self):
        assert self.rule().check(buy(qty="10"), make_ctx()).approved

    def test_partial_sell_of_long_approved(self):
        ctx = make_ctx(positions=[make_position("SPY", qty="10")])
        assert self.rule().check(sell(qty="5"), ctx).approved

    def test_full_sell_of_long_approved(self):
        ctx = make_ctx(positions=[make_position("SPY", qty="10")])
        assert self.rule().check(sell(qty="10"), ctx).approved

    def test_sell_exceeding_long_rejected(self):
        ctx = make_ctx(positions=[make_position("SPY", qty="10")])
        result = self.rule().check(sell(qty="15"), ctx)
        assert result.verdict == RiskVerdict.REJECTED
        assert result.detail["projected_qty"] == "-5"

    def test_sell_while_flat_rejected(self):
        result = self.rule().check(sell(qty="1"), make_ctx(positions=[]))
        assert result.verdict == RiskVerdict.REJECTED

    def test_close_of_long_approved(self):
        ctx = make_ctx(positions=[make_position("SPY", qty="10")])
        assert self.rule().check(close(), ctx).approved


def test_default_rules_order_is_deterministic():
    settings = make_settings()
    names = [r.name for r in default_rules(settings)]
    assert names == EXPECTED_RULE_ORDER
    assert [type(r) for r in default_rules(settings)] == [
        type(r) for r in default_rules(settings)
    ]


# ================================================================ engine


async def _events(session, rule: str | None = None) -> list[RiskEvent]:
    stmt = sa.select(RiskEvent).order_by(RiskEvent.created_at)
    if rule is not None:
        stmt = stmt.where(RiskEvent.rule == rule)
    return list((await session.execute(stmt)).scalars().all())


async def test_engine_clean_context_approves_and_persists_events(db_session):
    engine = RiskEngine(make_settings())
    decision = await engine.evaluate(db_session, buy(), make_ctx())

    assert decision.approved
    assert decision.verdict == RiskVerdict.APPROVED
    assert [r.rule for r in decision.results] == EXPECTED_RULE_ORDER
    assert all(r.approved for r in decision.results)

    events = await _events(db_session)
    assert sorted(e.rule for e in events) == sorted(EXPECTED_RULE_ORDER)
    assert all(e.severity == "info" for e in events)
    assert all(e.verdict == RiskVerdict.APPROVED for e in events)
    assert all(e.environment == Environment.PAPER for e in events)


async def test_engine_kill_switch_halts_everything(db_session):
    engine = RiskEngine(make_settings())
    await controls.engage_kill_switch(
        db_session, actor="tester", reason="manual stop", environment=Environment.PAPER
    )

    decision = await engine.evaluate(db_session, buy(), make_ctx())
    assert decision.verdict == RiskVerdict.HALTED
    assert len(decision.results) == 1  # nothing else evaluated
    assert decision.results[0].rule == "kill_switch"
    assert "manual stop" in decision.results[0].reason

    # One audit event from engaging + one persisted evaluation result.
    events = await _events(db_session, rule="kill_switch")
    assert len(events) == 2
    assert all(e.severity == "critical" for e in events)


async def test_engine_circuit_breaker_auto_trips_and_resets(db_session):
    settings = make_settings(
        risk_circuit_breaker_errors=3, risk_circuit_breaker_window_seconds=300
    )
    engine = RiskEngine(settings)
    for i in range(3):
        await controls.record_broker_error(
            db_session, Environment.PAPER, {"error": f"timeout {i}"}
        )
    real_now = utcnow()

    decision = await engine.evaluate(db_session, buy(), make_ctx(now=real_now))
    assert decision.verdict == RiskVerdict.HALTED
    assert len(decision.results) == 1
    assert decision.results[0].rule == "circuit_breaker"
    assert "auto-tripped" in decision.results[0].reason

    state = await controls.circuit_breaker_state(db_session)
    assert state["tripped"] is True
    assert "broker errors" in state["reason"]

    await controls.reset_circuit_breaker(
        db_session, actor="tester", reason="resolved", environment=Environment.PAPER
    )
    # Evaluate after the error window has passed: breaker stays clear.
    later = real_now + timedelta(seconds=400)
    decision2 = await engine.evaluate(db_session, buy(), make_ctx(now=later))
    assert decision2.verdict == RiskVerdict.APPROVED
    assert (await controls.circuit_breaker_state(db_session))["tripped"] is False


async def test_engine_below_error_threshold_does_not_trip(db_session):
    settings = make_settings(risk_circuit_breaker_errors=3)
    engine = RiskEngine(settings)
    await controls.record_broker_error(db_session, Environment.PAPER, {"error": "one-off"})

    decision = await engine.evaluate(db_session, buy(), make_ctx(now=utcnow()))
    assert decision.verdict == RiskVerdict.APPROVED
    assert (await controls.circuit_breaker_state(db_session)).get("tripped", False) is False


async def test_engine_rejects_duplicate_order_within_window(db_session):
    engine = RiskEngine(make_settings(risk_duplicate_window_seconds=60))
    db_session.add(
        Order(
            client_order_id="qp-dup-recent",
            environment=Environment.PAPER,
            symbol="SPY",
            side="buy",
            qty=Decimal(10),
            order_type=OrderType.MARKET,
            status=OrderStatus.SUBMITTED,
            created_at=NOW - timedelta(seconds=10),
        )
    )
    await db_session.flush()

    decision = await engine.evaluate(db_session, buy(), make_ctx(now=NOW))
    assert decision.verdict == RiskVerdict.REJECTED
    assert [r.rule for r in decision.results] == ["duplicate_order"]  # rules skipped

    events = await _events(db_session, rule="duplicate_order")
    assert len(events) == 1
    assert events[0].severity == "warning"
    assert events[0].verdict == RiskVerdict.REJECTED


async def test_engine_ignores_old_order_outside_window(db_session):
    engine = RiskEngine(make_settings(risk_duplicate_window_seconds=60))
    db_session.add(
        Order(
            client_order_id="qp-dup-old",
            environment=Environment.PAPER,
            symbol="SPY",
            side="buy",
            qty=Decimal(10),
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            created_at=NOW - timedelta(seconds=120),
        )
    )
    await db_session.flush()

    decision = await engine.evaluate(db_session, buy(), make_ctx(now=NOW))
    assert decision.verdict == RiskVerdict.APPROVED
    assert len(decision.results) == len(EXPECTED_RULE_ORDER)


async def test_engine_live_gate_blocks_ungated_config(db_session):
    engine = RiskEngine(make_settings())  # default paper settings
    ctx = make_ctx(env=Environment.LIVE)

    decision = await engine.evaluate(db_session, buy(), ctx)
    assert decision.verdict == RiskVerdict.HALTED
    assert len(decision.results) == 1
    assert decision.results[0].rule == "live_gate"
    assert "configuration gates" in decision.results[0].reason

    events = await _events(db_session, rule="live_gate")
    assert len(events) == 1
    assert events[0].severity == "critical"


async def test_engine_live_gate_requires_runtime_arming(db_session):
    settings = make_settings(
        trading_env="live",
        live_trading_enabled=True,
        live_trading_confirmation=LIVE_CONFIRMATION_PHRASE,
        alpaca_live_api_key="fake-live-key",
        alpaca_live_api_secret="fake-live-secret",
    )
    assert settings.live_trading_allowed()
    engine = RiskEngine(settings)
    ctx = make_ctx(env=Environment.LIVE)

    # Config gates pass but the runtime arm switch is unset: still halted.
    decision = await engine.evaluate(db_session, buy(), ctx)
    assert decision.verdict == RiskVerdict.HALTED
    assert decision.results[0].rule == "live_gate"
    assert "not armed" in decision.results[0].reason

    await controls.set_live_armed(db_session, True, actor="operator", reason="go-live window")
    decision2 = await engine.evaluate(db_session, buy(), ctx)
    assert decision2.verdict == RiskVerdict.APPROVED
    assert [r.rule for r in decision2.results] == EXPECTED_RULE_ORDER


async def test_engine_persists_rule_rejections_with_warning_severity(db_session):
    engine = RiskEngine(make_settings(risk_max_order_notional=5_000.0))
    # 500 shares * 100 = 50_000: breaks order notional, position, and exposure.
    decision = await engine.evaluate(db_session, buy(qty="500"), make_ctx())
    assert decision.verdict == RiskVerdict.REJECTED
    assert "order_notional" in [r.rule for r in decision.results if not r.approved]

    severity_by_rule = {e.rule: e.severity for e in await _events(db_session)}
    assert severity_by_rule["order_notional"] == "warning"
    assert severity_by_rule["position_limit"] == "warning"
    assert severity_by_rule["gross_exposure"] == "warning"
    assert severity_by_rule["qty_sanity"] == "info"  # approving rules stay info
    assert severity_by_rule["stale_data"] == "info"


# ================================================================ regressions
# Both bugs below were found on a live paper account that had accumulated
# 2.7x leverage and could not unwind. They are the reason the platform now
# treats "reduce risk" and "already working" as first-class concepts.


def test_order_notional_never_blocks_an_exit():
    """A position that grew beyond the cap must still be closable.

    Without this exemption every exit itself exceeds the per-order limit and
    is rejected, trapping the account in the exposure the cap exists to
    prevent.
    """
    from app.risk.rules import OrderNotionalRule

    settings = Settings(_env_file=None, risk_max_order_notional=1_000)
    rule = OrderNotionalRule(settings)
    ctx = make_ctx(
        positions=[make_position("SPY", "100", "500")],  # $50,000 position
        last_price="500",
    )
    # Closing all 100 shares is $50,000 — far above the $1,000 cap.
    closing = TradeIntent(symbol="SPY", action=SignalAction.CLOSE, qty=Decimal(100))
    assert rule.check(closing, ctx).approved

    # A partial sell is also risk-reducing.
    selling = TradeIntent(symbol="SPY", action=SignalAction.SELL, qty=Decimal(40))
    assert rule.check(selling, ctx).approved

    # But an oversized BUY is still rejected — the cap still does its job.
    buying = TradeIntent(symbol="SPY", action=SignalAction.BUY, qty=Decimal(10))
    assert not rule.check(buying, ctx).approved


async def test_working_order_blocks_re_entry_regardless_of_age(db_session):
    """The leverage-accumulation bug: an order queued overnight leaves the
    broker reporting no position, so a "buy when flat" strategy re-buys every
    loop. Age-independent detection is what stops it."""
    settings = Settings(_env_file=None, risk_duplicate_window_seconds=60)
    engine = RiskEngine(settings, rules=[])

    # An order placed hours ago that is still working at the broker.
    db_session.add(
        Order(
            client_order_id="qp-old-working",
            environment=Environment.PAPER,
            symbol="SPY",
            side="buy",
            qty=Decimal(10),
            order_type="market",
            status=OrderStatus.ACCEPTED,
            created_at=utcnow() - timedelta(hours=6),
        )
    )
    await db_session.flush()

    intent = TradeIntent(symbol="SPY", action=SignalAction.BUY, qty=Decimal(10))
    decision = await engine.evaluate(db_session, intent, make_ctx(now=utcnow()))
    assert not decision.approved
    assert any("still working" in r for r in decision.rejection_reasons)


async def test_old_filled_order_does_not_block_re_entry(db_session):
    """Only *live* orders block. A filled order from hours ago must not
    prevent a legitimate new entry, or the bot could never trade twice."""
    settings = Settings(_env_file=None, risk_duplicate_window_seconds=60)
    engine = RiskEngine(settings, rules=[])

    db_session.add(
        Order(
            client_order_id="qp-old-filled",
            environment=Environment.PAPER,
            symbol="SPY",
            side="buy",
            qty=Decimal(10),
            order_type="market",
            status=OrderStatus.FILLED,
            created_at=utcnow() - timedelta(hours=6),
        )
    )
    await db_session.flush()

    intent = TradeIntent(symbol="SPY", action=SignalAction.BUY, qty=Decimal(10))
    decision = await engine.evaluate(db_session, intent, make_ctx(now=utcnow()))
    assert decision.approved
