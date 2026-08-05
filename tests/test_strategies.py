"""Tests for the builtin example strategies and the strategy registry.

Covers registry loading/instantiation from DB rows, SMA-crossover entry/exit
logic, Wilder-RSI mean-reversion logic, safe behavior on short (pre-warmup)
input, determinism, and parameter validation.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.db.models import SignalAction
from app.db.models import Strategy as StrategyRow
from app.schemas.core import BarData, PositionState
from app.strategies import registry
from app.strategies.base import StrategyContext
from app.strategies.builtin.rsi_reversion import RsiReversion
from app.strategies.builtin.sma_cross import SmaCross
from tests.conftest import make_bars, make_position

SMA_PATH = "app.strategies.builtin.sma_cross.SmaCross"
RSI_PATH = "app.strategies.builtin.rsi_reversion.RsiReversion"


def bars_with_closes(closes: list[float | int], symbol: str = "SPY") -> list[BarData]:
    """Deterministic bars whose closes are hand-set (BarData is frozen: copy)."""
    bars = make_bars(symbol=symbol, n=len(closes))
    return [
        bar.model_copy(update={"close": Decimal(str(c))})
        for bar, c in zip(bars, closes, strict=True)
    ]


def ctx_for(
    bars: list[BarData], position: PositionState | None = None, symbol: str = "SPY"
) -> StrategyContext:
    return StrategyContext(
        symbol=symbol, bars=bars, position=position, equity=Decimal("100000")
    )


# ---------------------------------------------------------------- registry


def test_load_builtins_registers_both() -> None:
    registry.load_builtins()
    known = registry.known_strategies()
    assert known[SMA_PATH] is SmaCross
    assert known[RSI_PATH] is RsiReversion


async def test_instantiate_from_db_row(db_session) -> None:
    registry.load_builtins()
    db_session.add(
        StrategyRow(name="sma_cross", version=1, class_path=SMA_PATH, params={"fast": 3, "slow": 5})
    )
    await db_session.commit()

    row = (await db_session.execute(sa.select(StrategyRow))).scalar_one()
    strat = registry.instantiate(row)
    assert isinstance(strat, SmaCross)
    assert strat.params == {"fast": 3, "slow": 5}
    assert strat.warmup_bars() == 6


def test_unknown_class_path_raises() -> None:
    row = StrategyRow(name="nope", class_path="app.strategies.builtin.nope.Nope", params={})
    with pytest.raises(ValueError, match="not registered"):
        registry.instantiate(row)


# ---------------------------------------------------------------- SmaCross

# With fast=3 / slow=5 (warmup 6): six flat closes then a jump/drop makes the
# fast SMA cross the slow SMA exactly on the last bar.
CROSS_UP_CLOSES = [10, 10, 10, 10, 10, 10, 15]
CROSS_DOWN_CLOSES = [10, 10, 10, 10, 10, 10, 5]
SMA_PARAMS = {"fast": 3, "slow": 5, "qty": 2}


def test_sma_cross_up_while_flat_buys() -> None:
    strat = SmaCross(params=SMA_PARAMS)
    intents = strat.on_bars(ctx_for(bars_with_closes(CROSS_UP_CLOSES)))
    assert len(intents) == 1
    intent = intents[0]
    assert intent.action == SignalAction.BUY
    assert intent.qty == Decimal(2)
    assert intent.symbol == "SPY"


def test_sma_cross_up_while_long_does_not_rebuy() -> None:
    strat = SmaCross(params=SMA_PARAMS)
    ctx = ctx_for(bars_with_closes(CROSS_UP_CLOSES), position=make_position(qty="5"))
    assert strat.on_bars(ctx) == []


def test_sma_cross_down_while_long_closes_full_position() -> None:
    strat = SmaCross(params=SMA_PARAMS)
    ctx = ctx_for(bars_with_closes(CROSS_DOWN_CLOSES), position=make_position(qty="7"))
    intents = strat.on_bars(ctx)
    assert len(intents) == 1
    intent = intents[0]
    assert intent.action == SignalAction.CLOSE
    assert intent.qty == Decimal(7)
    assert intent.symbol == "SPY"


def test_sma_cross_down_while_flat_does_nothing() -> None:
    strat = SmaCross(params=SMA_PARAMS)
    assert strat.on_bars(ctx_for(bars_with_closes(CROSS_DOWN_CLOSES))) == []


def test_sma_no_cross_does_nothing() -> None:
    # Steadily rising closes: fast SMA is above slow SMA on both bars — no cross.
    strat = SmaCross(params=SMA_PARAMS)
    assert strat.on_bars(ctx_for(make_bars(n=30))) == []


def test_sma_short_input_is_safe() -> None:
    strat = SmaCross(params=SMA_PARAMS)  # warmup_bars == 6
    assert strat.warmup_bars() == 6
    assert strat.on_bars(ctx_for(bars_with_closes([10, 10, 10, 15]))) == []


def test_sma_default_params() -> None:
    assert SmaCross().warmup_bars() == 21  # slow=20 default


# ---------------------------------------------------------------- RsiReversion


def test_rsi_falling_series_buys_when_flat() -> None:
    strat = RsiReversion(params={"qty": 3})
    bars = make_bars(n=20, start_price=100.0, step=-1.0)  # all losses -> RSI 0
    intents = strat.on_bars(ctx_for(bars))
    assert len(intents) == 1
    intent = intents[0]
    assert intent.action == SignalAction.BUY
    assert intent.qty == Decimal(3)
    assert intent.symbol == "SPY"


def test_rsi_falling_series_no_rebuy_when_long() -> None:
    strat = RsiReversion(params={"qty": 3})
    bars = make_bars(n=20, start_price=100.0, step=-1.0)
    assert strat.on_bars(ctx_for(bars, position=make_position(qty="3"))) == []


def test_rsi_rising_series_closes_when_long() -> None:
    strat = RsiReversion()
    bars = make_bars(n=20)  # all gains -> RSI 100
    intents = strat.on_bars(ctx_for(bars, position=make_position(qty="4")))
    assert len(intents) == 1
    intent = intents[0]
    assert intent.action == SignalAction.CLOSE
    assert intent.qty == Decimal(4)


def test_rsi_rising_series_does_nothing_when_flat() -> None:
    strat = RsiReversion()
    assert strat.on_bars(ctx_for(make_bars(n=20))) == []


def test_rsi_midrange_does_nothing() -> None:
    # Alternating +1/-1 closes: equal gains and losses -> RSI exactly 50.
    strat = RsiReversion()
    closes = [100 + (i % 2) for i in range(15)]  # period 14 + 1 closes
    bars = bars_with_closes(closes)
    assert strat.on_bars(ctx_for(bars)) == []
    assert strat.on_bars(ctx_for(bars, position=make_position(qty="4"))) == []


def test_rsi_short_input_is_safe() -> None:
    strat = RsiReversion()  # warmup_bars == 15
    assert strat.warmup_bars() == 15
    assert strat.on_bars(ctx_for(make_bars(n=10, step=-1.0))) == []


# ---------------------------------------------------------------- determinism


def test_sma_cross_is_deterministic() -> None:
    strat = SmaCross(params=SMA_PARAMS)
    ctx = ctx_for(bars_with_closes(CROSS_UP_CLOSES))
    assert strat.on_bars(ctx) == strat.on_bars(ctx)


def test_rsi_reversion_is_deterministic() -> None:
    strat = RsiReversion(params={"qty": 3})
    ctx = ctx_for(make_bars(n=20, start_price=100.0, step=-1.0))
    assert strat.on_bars(ctx) == strat.on_bars(ctx)


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "params",
    [
        {"fast": 5, "slow": 5},  # fast must be strictly < slow
        {"fast": 10, "slow": 5},
        {"fast": 0, "slow": 5},
        {"fast": -3, "slow": 5},
        {"qty": 0},
        {"qty": -1},
        {"fast": "10"},  # ints only
        {"fast": 2.5},
        {"qty": True},
    ],
)
def test_sma_param_validation_raises(params: dict) -> None:
    with pytest.raises(ValueError):
        SmaCross(params=params)


@pytest.mark.parametrize(
    "params",
    [
        {"oversold": 70, "overbought": 70},  # oversold must be strictly < overbought
        {"oversold": 80, "overbought": 20},
        {"oversold": 0, "overbought": 70},
        {"oversold": 30, "overbought": 100},
        {"period": 1},
        {"period": "14"},
        {"qty": 0},
        {"qty": -2},
    ],
)
def test_rsi_param_validation_raises(params: dict) -> None:
    with pytest.raises(ValueError):
        RsiReversion(params=params)
