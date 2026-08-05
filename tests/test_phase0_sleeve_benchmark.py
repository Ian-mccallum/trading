"""Phase 0 tests: benchmark feature injection and the target-weight sleeve.

The sleeve is the mechanism that lets multi-asset portfolios (All Weather,
Permanent Portfolio) be expressed one symbol at a time, so its arithmetic is
tested against hand-computed values rather than approximations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.models import SignalAction
from app.schemas.core import BarData, PositionState
from app.strategies.base import StrategyContext
from app.strategies.benchmark import (
    FEATURE_KEY,
    build_benchmark_features,
    read_benchmark,
    relative_return,
)
from app.strategies.builtin.target_weight_sleeve import TargetWeightSleeve
from tests.conftest import make_bars

# ---------------------------------------------------------------- helpers


def bars_at(price: float, n: int = 5, symbol: str = "VTI") -> list[BarData]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        BarData(
            symbol=symbol, timeframe="1Day", ts=start + timedelta(days=i),
            open=Decimal(str(price)), high=Decimal(str(price)),
            low=Decimal(str(price)), close=Decimal(str(price)),
        )
        for i in range(n)
    ]


def position_at(qty: str, price: str, symbol: str = "VTI") -> PositionState:
    q, p = Decimal(qty), Decimal(price)
    return PositionState(
        symbol=symbol, qty=q, avg_entry_price=p,
        market_value=q * p, unrealized_pl=Decimal(0),
    )


def ctx(bars, position=None, equity="100000", symbol="VTI") -> StrategyContext:
    return StrategyContext(
        symbol=symbol, bars=bars, position=position,
        equity=Decimal(equity), features={},
    )


# ---------------------------------------------------------------- benchmark


def test_build_benchmark_features_returns_lookbacks():
    bars = make_bars(n=300, start_price=100, step=0.5, symbol="SPY")
    features = build_benchmark_features("SPY", bars)
    assert features["symbol"] == "SPY"
    assert set(features["returns"]) == {"return_1m", "return_3m", "return_6m", "return_12m"}
    assert all(v > 0 for v in features["returns"].values())  # uptrend


def test_build_benchmark_features_partial_history():
    # 40 bars: only the 21-day lookback is computable.
    features = build_benchmark_features("SPY", make_bars(n=40, symbol="SPY"))
    assert list(features["returns"]) == ["return_1m"]


def test_build_benchmark_features_insufficient_history():
    assert build_benchmark_features("SPY", make_bars(n=1)) == {}
    assert build_benchmark_features("SPY", []) == {}


def test_read_benchmark_roundtrip():
    features = {FEATURE_KEY: build_benchmark_features("SPY", make_bars(n=300, symbol="SPY"))}
    view = read_benchmark(features)
    assert view is not None
    assert view.symbol == "SPY"
    assert view.get_return("return_12m") is not None
    assert view.get_return("nonexistent") is None


@pytest.mark.parametrize(
    "features",
    [None, {}, {FEATURE_KEY: None}, {FEATURE_KEY: "nope"}, {FEATURE_KEY: {}},
     {FEATURE_KEY: {"symbol": "SPY"}}, {FEATURE_KEY: {"symbol": 1, "returns": {"a": 1}}},
     {FEATURE_KEY: {"symbol": "SPY", "returns": {"return_12m": True}}}],
)
def test_read_benchmark_malformed_returns_none(features):
    """Consumers must be able to trust None over a half-valid view."""
    assert read_benchmark(features) is None


def test_relative_return_outperformance_and_underperformance():
    strong = make_bars(n=300, start_price=100, step=1.0)   # steep uptrend
    weak = make_bars(n=300, start_price=300, step=0.05)    # barely rising
    bench = read_benchmark(
        {FEATURE_KEY: build_benchmark_features("SPY", make_bars(n=300, start_price=100, step=0.5))}
    )
    assert relative_return(strong, bench) > 0
    assert relative_return(weak, bench) < 0


def test_relative_return_missing_inputs():
    assert relative_return(make_bars(n=300), None) is None  # no benchmark
    bench = read_benchmark(
        {FEATURE_KEY: build_benchmark_features("SPY", make_bars(n=300, symbol="SPY"))}
    )
    assert relative_return(make_bars(n=10), bench) is None  # own history too short
    assert relative_return(make_bars(n=300), bench, "return_99y") is None  # unknown key


# ---------------------------------------------------------------- sleeve: buys


def test_sleeve_buys_to_target_from_flat():
    # target 25% of 100k = 25,000 at price 100 -> 250 shares.
    s = TargetWeightSleeve({"target_weight": 0.25})
    intents = s.on_bars(ctx(bars_at(100.0)))
    assert len(intents) == 1
    assert intents[0].action == SignalAction.BUY
    assert intents[0].qty == Decimal(250)


def test_sleeve_buys_only_the_shortfall():
    # Holding 100 sh @100 = 10,000 (10%); target 25% -> buy 15,000 -> 150 sh.
    s = TargetWeightSleeve({"target_weight": 0.25})
    intents = s.on_bars(ctx(bars_at(100.0), position_at("100", "100")))
    assert intents[0].qty == Decimal(150)


def test_relative_band_scales_with_small_targets():
    """The All Weather bug: a 0.10 absolute band exceeds a 7.5% target, so the
    leg could never be bought. The default relative band scales correctly."""
    small = TargetWeightSleeve({"target_weight": 0.075})  # band_rel 0.25 -> 0.01875
    intents = small.on_bars(ctx(bars_at(50.0)))
    assert intents[0].action == SignalAction.BUY
    assert intents[0].qty == Decimal(150)  # 7.5% of 100k = 7,500 / 50

    with pytest.raises(ValueError, match="could never be bought"):
        TargetWeightSleeve({"target_weight": 0.075, "band_abs": 0.10})


def test_browne_absolute_band_still_expressible():
    """Permanent Portfolio's canonical 15-35% rule = target .25, band_abs .10."""
    s = TargetWeightSleeve({"target_weight": 0.25, "band_abs": 0.10})
    # 20% held is inside 15-35% -> no action.
    assert s.on_bars(ctx(bars_at(100.0), position_at("200", "100"))) == []
    # 36% held is outside -> sell back toward target.
    intents = s.on_bars(ctx(bars_at(100.0), position_at("360", "100")))
    assert intents[0].action == SignalAction.SELL


def test_sleeve_within_band_does_nothing():
    # Holding 20% against a 25% target with a 0.10 band -> inside, no action.
    s = TargetWeightSleeve({"target_weight": 0.25, "band_abs": 0.10})
    assert s.on_bars(ctx(bars_at(100.0), position_at("200", "100"))) == []


def test_sleeve_band_boundary_is_exclusive():
    # Exactly at the band edge (15% vs 25% target, band 0.10) -> no action.
    s = TargetWeightSleeve({"target_weight": 0.25, "band_abs": 0.10})
    assert s.on_bars(ctx(bars_at(100.0), position_at("150", "100"))) == []


def test_sleeve_rounds_down_to_whole_shares():
    # 25% of 100k = 25,000 at price 300 -> 83.33 -> 83 shares.
    s = TargetWeightSleeve({"target_weight": 0.25})
    intents = s.on_bars(ctx(bars_at(300.0)))
    assert intents[0].qty == Decimal(83)


# ---------------------------------------------------------------- sleeve: sells


def test_sleeve_sells_excess_when_overweight():
    # Holding 400 sh @100 = 40,000 (40%); target 25% -> sell 15,000 -> 150 sh.
    s = TargetWeightSleeve({"target_weight": 0.25, "band_abs": 0.10})
    intents = s.on_bars(ctx(bars_at(100.0), position_at("400", "100")))
    assert len(intents) == 1
    assert intents[0].action == SignalAction.SELL
    assert intents[0].qty == Decimal(150)


def test_sleeve_never_sells_more_than_held():
    """Long-only guard: even absurd equity/position combinations cannot short."""
    s = TargetWeightSleeve({"target_weight": 0.01, "band_abs": 0.0})
    position = position_at("5", "100")  # only 5 shares held
    intents = s.on_bars(ctx(bars_at(100.0), position, equity="1000"))
    assert intents[0].action == SignalAction.SELL
    assert intents[0].qty <= position.qty


def test_sleeve_suppresses_dust_trades():
    # 249 sh @ $100 = 24.9% vs a 25% target on 100k = a $100 shortfall (1 share).
    # Below a $150 floor it is suppressed; above it, it trades.
    overrides = {"target_weight": 0.25, "band_abs": 0.0}
    dusty = TargetWeightSleeve({**overrides, "min_trade_notional": 150.0})
    assert dusty.on_bars(ctx(bars_at(100.0), position_at("249", "100"))) == []

    permissive = TargetWeightSleeve({**overrides, "min_trade_notional": 50.0})
    intents = permissive.on_bars(ctx(bars_at(100.0), position_at("249", "100")))
    assert intents[0].qty == Decimal(1)


# ---------------------------------------------------------------- sleeve: regime filter


def test_sleeve_regime_filter_flattens_below_sma():
    # Downtrend: last close below the 50-bar SMA -> full exit.
    bars = make_bars(n=60, start_price=200, step=-1.0)
    s = TargetWeightSleeve({"target_weight": 0.25, "regime_filter_sma": 50})
    intents = s.on_bars(ctx(bars, position_at("100", "100")))
    assert len(intents) == 1
    assert intents[0].action == SignalAction.CLOSE
    assert intents[0].qty == Decimal(100)
    assert intents[0].context["regime"] == "below_sma_defensive"


def test_sleeve_regime_filter_no_position_below_sma_is_noop():
    bars = make_bars(n=60, start_price=200, step=-1.0)
    s = TargetWeightSleeve({"target_weight": 0.25, "regime_filter_sma": 50})
    assert s.on_bars(ctx(bars)) == []


def test_sleeve_regime_filter_allows_buys_above_sma():
    bars = make_bars(n=60, start_price=100, step=1.0)  # uptrend
    s = TargetWeightSleeve({"target_weight": 0.25, "regime_filter_sma": 50})
    intents = s.on_bars(ctx(bars))
    assert intents[0].action == SignalAction.BUY


def test_sleeve_regime_filter_waits_for_history():
    s = TargetWeightSleeve({"target_weight": 0.25, "regime_filter_sma": 200})
    assert s.on_bars(ctx(make_bars(n=50))) == []


# ---------------------------------------------------------------- sleeve: edges & params


def test_sleeve_handles_degenerate_inputs():
    s = TargetWeightSleeve({"target_weight": 0.25})
    assert s.on_bars(ctx([])) == []                       # no bars
    assert s.on_bars(ctx(bars_at(100.0), equity="0")) == []  # zero equity
    assert s.on_bars(ctx(bars_at(0.0))) == []             # zero price


def test_sleeve_determinism():
    s = TargetWeightSleeve({"target_weight": 0.25})
    c = ctx(bars_at(100.0))
    assert s.on_bars(c) == s.on_bars(c)


@pytest.mark.parametrize(
    "params,match",
    [
        ({"target_weight": 0}, "target_weight"),
        ({"target_weight": 1.5}, "target_weight"),
        ({"target_weight": 0.25, "band_abs": 1.0}, "band_abs"),
        ({"target_weight": 0.25, "band_abs": -0.1}, "band_abs"),
        ({"target_weight": 0.25, "band_rel": 1.0}, "band_rel"),
        # A band wider than the target makes the sleeve permanently unbuyable.
        ({"target_weight": 0.075, "band_abs": 0.10}, "could never be bought"),
        ({"target_weight": 0.25, "min_trade_notional": -1}, "min_trade_notional"),
        ({"target_weight": 0.25, "regime_filter_sma": True}, "regime_filter_sma"),
        ({"target_weight": 0.25, "regime_filter_sma": 1}, "regime_filter_sma"),
        ({"target_weight": 0.25, "max_weight": 0.5}, "max_weight"),
    ],
)
def test_sleeve_param_validation(params, match):
    with pytest.raises(ValueError, match=match):
        TargetWeightSleeve(params)


# ---------------------------------------------------------------- portfolio composition


def test_permanent_portfolio_weights_compose():
    """Four 25% sleeves each buy their own leg to target from flat."""
    legs = {"VTI": 0.25, "TLT": 0.25, "GLD": 0.25, "BIL": 0.25}
    total_notional = Decimal(0)
    for symbol, weight in legs.items():
        s = TargetWeightSleeve({"target_weight": weight})
        intents = s.on_bars(ctx(bars_at(100.0, symbol=symbol), symbol=symbol))
        assert len(intents) == 1
        total_notional += intents[0].qty * Decimal(100)
    assert total_notional == Decimal(100_000)  # fully allocated, no leverage


def test_all_weather_weights_compose_without_leverage():
    legs = {"VTI": 0.30, "TLT": 0.40, "IEI": 0.15, "GLD": 0.075, "DBC": 0.075}
    assert sum(legs.values()) == pytest.approx(1.0)
    total = Decimal(0)
    for symbol, weight in legs.items():
        s = TargetWeightSleeve({"target_weight": weight})
        intents = s.on_bars(ctx(bars_at(50.0, symbol=symbol), symbol=symbol))
        total += intents[0].qty * Decimal(50)
    # Whole-share rounding may leave a small cash residual, never an overdraft.
    assert total <= Decimal(100_000)
    assert total > Decimal(99_000)


def test_sleeve_registered_in_registry():
    from app.strategies import registry

    registry.load_builtins()
    assert (
        "app.strategies.builtin.target_weight_sleeve.TargetWeightSleeve"
        in registry.known_strategies()
    )


# ---------------------------------------------------------------- symbol binding


def test_sleeve_ignores_other_symbols():
    """The worker offers every strategy every symbol; a bound sleeve must only
    act on its own leg, or a 30% sleeve would buy 30% of everything."""
    s = TargetWeightSleeve({"symbol": "VTI", "target_weight": 0.30})
    assert s.on_bars(ctx(bars_at(100.0, symbol="TLT"), symbol="TLT")) == []
    intents = s.on_bars(ctx(bars_at(100.0, symbol="VTI"), symbol="VTI"))
    assert intents[0].action == SignalAction.BUY


def test_sleeve_symbol_binding_is_case_insensitive():
    s = TargetWeightSleeve({"symbol": "vti", "target_weight": 0.30})
    assert s.on_bars(ctx(bars_at(100.0), symbol="VTI")) != []


def test_sleeve_unbound_acts_on_any_symbol():
    """Unbound sleeves are for backtests, which drive one symbol explicitly."""
    s = TargetWeightSleeve({"target_weight": 0.30})
    assert s.on_bars(ctx(bars_at(100.0, symbol="ANY"), symbol="ANY")) != []


@pytest.mark.parametrize("bad", ["", "   ", 123, True])
def test_sleeve_symbol_param_validation(bad):
    with pytest.raises(ValueError, match="symbol"):
        TargetWeightSleeve({"symbol": bad, "target_weight": 0.25})
