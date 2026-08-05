"""Tests for the simulated broker, backtest engine (no-lookahead), metrics,
and backtest result persistence."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.backtest import metrics as m
from app.backtest.engine import BacktestConfig, BacktestEngine, save_backtest_result
from app.brokers.base import BrokerRejectionError
from app.brokers.simulated import SimulatedBroker
from app.db.models import (
    DecisionMode,
    Environment,
    Experiment,
    ExperimentKind,
    ExperimentStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    PerformanceReport,
    SignalAction,
)
from app.db.models import (
    Strategy as StrategyRow,
)
from app.schemas.core import OrderRequest, TradeIntent
from app.strategies.base import Strategy, StrategyContext
from tests.conftest import make_bars

TS = datetime(2024, 1, 1, tzinfo=UTC)


def _order(
    coid: str,
    side: OrderSide,
    qty: str,
    order_type: OrderType = OrderType.MARKET,
    limit: str | None = None,
    symbol: str = "SPY",
) -> OrderRequest:
    return OrderRequest(
        client_order_id=coid,
        symbol=symbol,
        side=side,
        qty=Decimal(qty),
        order_type=order_type,
        limit_price=Decimal(limit) if limit is not None else None,
    )


def _intent(symbol: str, action: SignalAction, qty: int = 1) -> TradeIntent:
    return TradeIntent(
        symbol=symbol, action=action, qty=Decimal(qty), mode=DecisionMode.BACKTEST
    )


def _curve(*values: float) -> list[tuple[datetime, Decimal]]:
    return [
        (TS + timedelta(days=i), Decimal(str(v))) for i, v in enumerate(values)
    ]


# ---------------------------------------------------------------- test strategies


class BuyOnceHold(Strategy):
    """Buys ``qty`` shares on the very first bar, then holds forever."""

    name = "test_buy_once_hold"

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        if len(ctx.bars) == 1 and ctx.position is None:
            return [_intent(ctx.symbol, SignalAction.BUY, int(self.params.get("qty", 1)))]
        return []


class BuyAtIndex(Strategy):
    """Emits a single buy intent exactly on bar index ``params['index']``."""

    name = "test_buy_at_index"

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        if len(ctx.bars) - 1 == int(self.params["index"]):
            return [_intent(ctx.symbol, SignalAction.BUY, int(self.params.get("qty", 1)))]
        return []


class BuyThenClose(Strategy):
    """Buys 3 shares on bar 0, emits a close on bar 3."""

    name = "test_buy_then_close"

    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        i = len(ctx.bars) - 1
        if i == 0 and ctx.position is None:
            return [_intent(ctx.symbol, SignalAction.BUY, 3)]
        if i == 3 and ctx.position is not None:
            return [_intent(ctx.symbol, SignalAction.CLOSE, 3)]
        return []


# ---------------------------------------------------------------- SimulatedBroker


async def test_market_fill_slippage_and_commission_exact():
    broker = SimulatedBroker(
        Decimal("10000"),
        slippage_bps=Decimal("5"),
        commission_per_share=Decimal("0.01"),
    )
    broker.set_clock(TS)
    broker.set_price("SPY", Decimal("100"))

    state = await broker.submit_order(_order("buy1", OrderSide.BUY, "10"))
    assert state.status == OrderStatus.FILLED
    # buy: 100 * (1 + 5/10000) = 100.05, exactly.
    assert state.filled_avg_price == Decimal("100.05")
    account = await broker.get_account()
    # 10000 - 100.05*10 - 0.01*10 = 8999.4
    assert account.cash == Decimal("8999.4")

    state = await broker.submit_order(_order("sell1", OrderSide.SELL, "10"))
    # sell: 100 * (1 - 5/10000) = 99.95, exactly.
    assert state.filled_avg_price == Decimal("99.95")
    account = await broker.get_account()
    # 8999.4 + 99.95*10 - 0.1 = 9998.8; flat, so equity == cash.
    assert account.cash == Decimal("9998.8")
    assert account.equity == Decimal("9998.8")
    assert await broker.get_positions() == []


async def test_limit_order_fills_only_when_crossed():
    broker = SimulatedBroker(Decimal("100000"))
    broker.set_clock(TS)
    broker.set_price("SPY", Decimal("101"))

    state = await broker.submit_order(
        _order("lb1", OrderSide.BUY, "10", OrderType.LIMIT, limit="100")
    )
    assert state.status == OrderStatus.ACCEPTED
    assert len(await broker.list_open_orders()) == 1

    broker.set_price("SPY", Decimal("100.5"))  # still above limit
    state = await broker.get_order("lb1")
    assert state is not None and state.status == OrderStatus.ACCEPTED

    broker.set_price("SPY", Decimal("99.5"))  # crossed: fills at market
    state = await broker.get_order("lb1")
    assert state is not None and state.status == OrderStatus.FILLED
    assert state.filled_avg_price == Decimal("99.5")
    assert await broker.list_open_orders() == []

    # Limit sell rests until price rises through the limit.
    state = await broker.submit_order(
        _order("ls1", OrderSide.SELL, "10", OrderType.LIMIT, limit="105")
    )
    assert state.status == OrderStatus.ACCEPTED
    broker.set_price("SPY", Decimal("104"))
    state = await broker.get_order("ls1")
    assert state is not None and state.status == OrderStatus.ACCEPTED
    broker.set_price("SPY", Decimal("105"))
    state = await broker.get_order("ls1")
    assert state is not None and state.status == OrderStatus.FILLED
    assert state.filled_avg_price == Decimal("105")
    assert await broker.get_positions() == []


async def test_long_only_sell_rejection():
    broker = SimulatedBroker(Decimal("10000"))
    broker.set_clock(TS)
    broker.set_price("SPY", Decimal("100"))

    with pytest.raises(BrokerRejectionError):
        await broker.submit_order(_order("s0", OrderSide.SELL, "1"))

    await broker.submit_order(_order("b1", OrderSide.BUY, "5"))
    with pytest.raises(BrokerRejectionError):
        await broker.submit_order(_order("s1", OrderSide.SELL, "6"))
    state = await broker.submit_order(_order("s2", OrderSide.SELL, "5"))
    assert state.status == OrderStatus.FILLED


async def test_cash_limit_buy_rejection():
    broker = SimulatedBroker(Decimal("1000"))
    broker.set_clock(TS)
    broker.set_price("SPY", Decimal("100"))

    with pytest.raises(BrokerRejectionError):
        await broker.submit_order(_order("b1", OrderSide.BUY, "11"))  # 1100 > 1000
    # Worst-case cost of a resting buy limit is also checked up front.
    with pytest.raises(BrokerRejectionError):
        await broker.submit_order(
            _order("b2", OrderSide.BUY, "20", OrderType.LIMIT, limit="90")
        )
    state = await broker.submit_order(_order("b3", OrderSide.BUY, "10"))  # exactly 1000
    assert state.status == OrderStatus.FILLED
    assert (await broker.get_account()).cash == Decimal("0")


async def test_submit_order_idempotent_on_client_order_id():
    broker = SimulatedBroker(Decimal("10000"))
    broker.set_clock(TS)
    broker.set_price("SPY", Decimal("100"))

    first = await broker.submit_order(_order("dup", OrderSide.BUY, "5"))
    second = await broker.submit_order(_order("dup", OrderSide.BUY, "5"))
    assert second.broker_order_id == first.broker_order_id
    assert second.status == OrderStatus.FILLED
    assert (await broker.get_account()).cash == Decimal("9500")  # deducted once
    assert len(await broker.list_fills(since=TS)) == 1
    assert await broker.list_fills(since=TS + timedelta(days=1)) == []


async def test_equity_marks_positions_to_market():
    broker = SimulatedBroker(Decimal("10000"))
    broker.set_clock(TS)
    broker.set_price("SPY", Decimal("100"))
    await broker.submit_order(_order("b1", OrderSide.BUY, "10"))

    broker.set_price("SPY", Decimal("110"))
    account = await broker.get_account()
    assert account.cash == Decimal("9000")
    assert account.equity == Decimal("10100")  # 9000 + 10 * 110
    (pos,) = await broker.get_positions()
    assert pos.qty == Decimal("10")
    assert pos.avg_entry_price == Decimal("100")
    assert pos.market_value == Decimal("1100")
    assert pos.unrealized_pl == Decimal("100")


async def test_cancel_resting_limit_order():
    broker = SimulatedBroker(Decimal("10000"))
    broker.set_clock(TS)
    broker.set_price("SPY", Decimal("101"))
    await broker.submit_order(_order("lb1", OrderSide.BUY, "1", OrderType.LIMIT, limit="100"))
    await broker.cancel_order("lb1")
    state = await broker.get_order("lb1")
    assert state is not None and state.status == OrderStatus.CANCELED
    assert await broker.list_open_orders() == []
    with pytest.raises(BrokerRejectionError):
        await broker.cancel_order("lb1")  # already terminal
    with pytest.raises(BrokerRejectionError):
        await broker.cancel_order("nope")  # unknown


# ---------------------------------------------------------------- engine


async def test_engine_no_lookahead_fills_at_next_open():
    # make_bars: bar i has open = 100 + i, close = 101 + i.
    bars = make_bars(n=8)
    result = await BacktestEngine(BacktestConfig()).run(BuyAtIndex({"index": 3}), bars)
    assert len(result.trades) == 1
    trade = result.trades[0]
    # Decided on bar 3 -> filled at bar 4's open = 104, never bar 3's prices.
    assert trade["price"] == Decimal("104") == bars[4].open
    assert trade["ts"] == bars[4].ts
    assert trade["side"] == "buy"


async def test_engine_discards_final_bar_intents():
    bars = make_bars(n=8)
    result = await BacktestEngine(BacktestConfig()).run(BuyAtIndex({"index": 7}), bars)
    assert result.trades == []
    assert result.final_equity == Decimal("100000")


async def test_engine_deterministic_end_to_end_equity():
    bars = make_bars(n=8)  # opens 100..107, closes 101..108
    engine = BacktestEngine(BacktestConfig())  # no slippage/commission
    result = await engine.run(BuyOnceHold({"qty": 5}), bars)

    # Buys 5 at bar 1 open = 101 (cost 505); holds to the last close = 108.
    # final = 100000 - 505 + 5 * 108 = 100035
    assert result.final_equity == Decimal("100035")
    assert len(result.equity_curve) == 8
    assert result.equity_curve[0][1] == Decimal("100000")  # flat during bar 0
    assert result.equity_curve[1][1] == Decimal("100005")  # 99495 + 5 * 102
    assert result.metrics["final_equity"] == 100035.0
    assert result.metrics["num_trades"] == 1
    assert result.config is engine.config


async def test_engine_close_action_flattens_position():
    bars = make_bars(n=6)
    result = await BacktestEngine(BacktestConfig()).run(BuyThenClose(), bars)

    assert len(result.trades) == 2
    buy, sell = result.trades
    assert (buy["side"], buy["qty"], buy["price"]) == ("buy", Decimal(3), Decimal("101"))
    assert (sell["side"], sell["qty"], sell["price"]) == ("sell", Decimal(3), Decimal("104"))
    assert sell["ts"] == bars[4].ts
    # Flat after the close: 100000 + 3 * (104 - 101) = 100009, equity == cash.
    assert result.final_equity == Decimal("100009")
    assert result.equity_curve[-1][1] == result.equity_curve[-2][1] == Decimal("100009")
    assert result.metrics["win_rate"] == 1.0  # one round trip, +9


# ---------------------------------------------------------------- metrics


def test_total_return_and_max_drawdown_hand_computed():
    curve = _curve(100, 110, 99, 121)
    assert m.total_return(curve) == pytest.approx(0.21)
    assert m.max_drawdown(curve) == pytest.approx((110 - 99) / 110)

    flat = _curve(100, 100, 100)
    assert m.total_return(flat) == 0.0
    assert m.max_drawdown(flat) == 0.0
    assert m.total_return([]) == 0.0
    assert m.max_drawdown([]) == 0.0


def test_sharpe_hand_computed():
    # Returns: [0.10, 0.05]; mean 0.075, sample stdev sqrt(0.00125).
    expected = 0.075 / math.sqrt(0.00125) * math.sqrt(252)
    assert m.sharpe(_curve(100, 110, 115.5)) == pytest.approx(expected)
    # Zero-mean returns -> Sharpe ~ 0.
    assert abs(m.sharpe(_curve(100, 110, 99))) < 1e-9
    # Degenerate: too few points or zero volatility.
    assert m.sharpe(_curve(100, 110)) == 0.0
    assert m.sharpe(_curve(100, 100, 100)) == 0.0
    assert m.sharpe([]) == 0.0


def test_win_rate_fifo_round_trips():
    trades = [
        {"ts": TS, "symbol": "SPY", "side": "buy", "qty": Decimal(10), "price": Decimal(100)},
        {"ts": TS, "symbol": "SPY", "side": "buy", "qty": Decimal(10), "price": Decimal(110)},
        {"ts": TS, "symbol": "SPY", "side": "sell", "qty": Decimal(15), "price": Decimal(105)},
    ]
    pnls = m.round_trip_pnls(trades)
    # FIFO: 10 @ 100 -> +50 (win), then 5 of the 110 lot -> -25 (loss).
    assert pnls == [50.0, -25.0]
    assert m.win_rate(pnls) == 0.5
    assert m.win_rate([]) == 0.0
    assert m.num_trades(trades) == 3


def test_compute_metrics_degenerate_inputs():
    empty = m.compute_metrics([], [])
    assert empty == {
        "final_equity": 0.0,
        "total_return": 0.0,
        "max_drawdown": 0.0,
        "sharpe": 0.0,
        "win_rate": 0.0,
        "num_trades": 0,
    }
    flat = m.compute_metrics(_curve(100, 100, 100), [])
    assert flat["total_return"] == 0.0
    assert flat["sharpe"] == 0.0
    assert flat["final_equity"] == 100.0


# ---------------------------------------------------------------- persistence


async def test_save_backtest_result_writes_report_and_experiment(db_session):
    row = StrategyRow(name="bt-test", version=1, class_path="tests.BuyOnceHold", params={})
    db_session.add(row)
    await db_session.flush()

    bars = make_bars(n=8)
    result = await BacktestEngine(BacktestConfig()).run(BuyOnceHold({"qty": 5}), bars)
    report, experiment = await save_backtest_result(
        db_session, row, result, period_start=bars[0].ts, period_end=bars[-1].ts
    )

    fetched_report = (
        await db_session.execute(sa.select(PerformanceReport))
    ).scalar_one()
    assert fetched_report.id == report.id
    assert fetched_report.strategy_id == row.id
    assert fetched_report.mode == DecisionMode.BACKTEST
    assert fetched_report.environment == Environment.PAPER
    assert fetched_report.metrics["final_equity"] == 100035.0
    assert fetched_report.metrics["num_trades"] == 1

    fetched_exp = (await db_session.execute(sa.select(Experiment))).scalar_one()
    assert fetched_exp.id == experiment.id
    assert fetched_exp.kind == ExperimentKind.BACKTEST
    assert fetched_exp.status == ExperimentStatus.COMPLETED
    assert fetched_exp.completed_at is not None
    assert fetched_exp.results["final_equity"] == 100035.0
    assert fetched_exp.results["summary"]["num_trades"] == 1
    assert fetched_exp.config["initial_cash"] == "100000"
