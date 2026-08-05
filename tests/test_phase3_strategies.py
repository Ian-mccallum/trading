"""Phase 3 tests: O'Neil technical subset, sweep reversal, dual momentum.

Recurring theme: each of these three strategies is a *partial* implementation
of its source, and the tests assert that the gaps are recorded rather than
silently papered over. A screen that ran with fewer checks than its name
implies must say so in the decision context.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.models import SignalAction
from app.schemas.core import BarData
from app.strategies.base import StrategyContext
from app.strategies.benchmark import (
    FEATURE_KEY,
    PEERS_KEY,
    build_benchmark_features,
    build_peer_features,
    read_peer,
)
from app.strategies.builtin.dual_momentum import DualMomentum
from app.strategies.builtin.oneil import ONeilBreakout
from app.strategies.builtin.sweep_reversal import SweepReversal
from tests.conftest import make_bars, make_position

# ---------------------------------------------------------------- helpers


def bars(rows: list[tuple[float, float, float, float]], symbol="AAPL") -> list[BarData]:
    """(open, high, low, close) tuples with a default volume of 1000."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        BarData(
            symbol=symbol, timeframe="1Day", ts=start + timedelta(days=i),
            open=Decimal(str(o)), high=Decimal(str(h)),
            low=Decimal(str(low)), close=Decimal(str(c)), volume=Decimal(1000),
        )
        for i, (o, h, low, c) in enumerate(rows)
    ]


def flat_bars(n: int, price: float = 100.0, volume: float = 1000.0, symbol="AAPL"):
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        BarData(
            symbol=symbol, timeframe="1Day", ts=start + timedelta(days=i),
            open=Decimal(str(price)), high=Decimal(str(price)),
            low=Decimal(str(price)), close=Decimal(str(price)),
            volume=Decimal(str(volume)),
        )
        for i in range(n)
    ]


def ctx(series, position=None, features=None, symbol="AAPL", equity="100000"):
    return StrategyContext(
        symbol=symbol, bars=series, position=position,
        equity=Decimal(equity), features=features or {},
    )


def bench(above_trend=True, step=0.1) -> dict:
    summary = build_benchmark_features(
        "SPY", make_bars(n=300, start_price=100, step=step, symbol="SPY")
    )
    summary["above_trend"] = above_trend
    return {FEATURE_KEY: summary}


def only(intents, action):
    assert len(intents) == 1, f"expected one intent, got {intents}"
    assert intents[0].action == action
    return intents[0]


# ================================================================ O'Neil


def breakout_series(volume_multiple: float = 2.0) -> list[BarData]:
    """A 60-bar base at 100, then a breakout close at 110 on heavy volume."""
    series = flat_bars(60, price=100.0, volume=1000.0)
    start = series[-1].ts + timedelta(days=1)
    series.append(
        BarData(
            symbol="AAPL", timeframe="1Day", ts=start,
            open=Decimal(101), high=Decimal(111), low=Decimal(101),
            close=Decimal(110), volume=Decimal(str(1000 * volume_multiple)),
        )
    )
    return series


def test_oneil_enters_on_volume_confirmed_breakout():
    s = ONeilBreakout({"require_leader": False, "qty": 2})
    intent = only(s.on_bars(ctx(breakout_series(), features=bench())), SignalAction.BUY)
    assert intent.qty == Decimal(2)
    assert float(intent.context["volume_ratio"]) >= 1.5
    assert "N,S" in intent.context["letters_evaluated"]


def test_oneil_requires_volume_surge():
    """A breakout on ordinary volume is the S letter failing."""
    s = ONeilBreakout({"require_leader": False})
    assert s.on_bars(ctx(breakout_series(volume_multiple=1.0), features=bench())) == []


def test_oneil_requires_new_high():
    """No breakout above the base means no entry regardless of volume."""
    s = ONeilBreakout({"require_leader": False})
    series = flat_bars(61, price=100.0, volume=5000.0)
    assert s.on_bars(ctx(series, features=bench())) == []


def test_oneil_market_filter_blocks_in_downtrend():
    """O'Neil's M: do not buy breakouts while the market is below trend."""
    s = ONeilBreakout({"require_leader": False})
    assert s.on_bars(ctx(breakout_series(), features=bench(above_trend=False))) == []


def test_oneil_records_absent_letters():
    """The audit row must show C, A and I were never evaluated."""
    s = ONeilBreakout({"require_leader": False})
    intent = only(s.on_bars(ctx(breakout_series(), features=bench())), SignalAction.BUY)
    assert intent.context["letters_absent"] == "C,A,I"


def test_oneil_flags_skipped_checks_without_benchmark():
    """No benchmark: M and L are skipped, and both say so."""
    s = ONeilBreakout({})
    intent = only(s.on_bars(ctx(breakout_series())), SignalAction.BUY)
    assert intent.context["market_filter_skipped"] == "true"
    assert intent.context["leader_check_skipped"] == "true"


def test_oneil_leader_check_blocks_laggards():
    """A symbol underperforming the benchmark fails the L letter.

    The base must be longer than the RS lookback (63 bars for return_3m) or
    the check is skipped for lack of history rather than actually evaluated.
    """
    series = flat_bars(100, price=100.0, volume=1000.0)
    series.append(
        BarData(
            symbol="AAPL", timeframe="1Day", ts=series[-1].ts + timedelta(days=1),
            open=Decimal(101), high=Decimal(111), low=Decimal(101),
            close=Decimal(110), volume=Decimal(3000),
        )
    )
    strong_market = bench(step=5.0)  # benchmark far outruns the flat symbol
    intents = ONeilBreakout({"require_leader": True}).on_bars(
        ctx(series, features=strong_market)
    )
    assert intents == []

    # Sanity: the same setup against a weak benchmark does pass the L letter,
    # proving the block above came from the comparison, not missing data.
    passing = ONeilBreakout({"require_leader": True}).on_bars(
        ctx(series, features=bench(step=0.001))
    )
    assert passing != []
    assert "L" in passing[0].context["letters_evaluated"]


def test_oneil_stop_is_checked_before_target():
    """A bar through both brackets must exit as a loss, not a win."""
    s = ONeilBreakout({})
    series = flat_bars(61, price=50.0)
    # Entry 100: close 50 is below the 8% stop and cannot be a 25% target.
    intent = only(
        s.on_bars(ctx(series, make_position(qty="1", price="100"))), SignalAction.CLOSE
    )
    assert intent.context["exit_reason"] == "stop"


def test_oneil_takes_profit_at_target():
    s = ONeilBreakout({})
    series = flat_bars(61, price=130.0)
    intent = only(
        s.on_bars(ctx(series, make_position(qty="1", price="100"))), SignalAction.CLOSE
    )
    assert intent.context["exit_reason"] == "target"


def test_oneil_holds_inside_brackets():
    s = ONeilBreakout({})
    series = flat_bars(61, price=105.0)
    assert s.on_bars(ctx(series, make_position(qty="1", price="100"))) == []


def test_oneil_target_can_be_disabled():
    s = ONeilBreakout({"target_pct": None})
    series = flat_bars(61, price=200.0)
    assert s.on_bars(ctx(series, make_position(qty="1", price="100"))) == []


@pytest.mark.parametrize(
    "params,match",
    [
        ({"base_lookback": 2}, "base_lookback"),
        ({"volume_multiple": 0.5}, "volume_multiple"),
        ({"stop_pct": 0}, "stop_pct"),
        ({"stop_pct": 1.2}, "stop_pct"),
        ({"rs_lookback": 3}, "rs_lookback"),
        ({"qty": 0}, "qty"),
    ],
)
def test_oneil_param_validation(params, match):
    with pytest.raises(ValueError, match=match):
        ONeilBreakout(params)


# ================================================================ sweep reversal


def sweep_series(*, close_above: bool = True, low_age: int = 10) -> list[BarData]:
    """A swing low `low_age` bars back, then a bar that pierces it and closes
    back above (or below, when close_above is False)."""
    rows = [(100, 101, 99, 100) for _ in range(30)]
    rows[-low_age] = (100, 101, 95, 100)  # the swing low being swept
    close = 96.5 if close_above else 94.0
    rows.append((97, 98, 94.0, close))  # pierces 95, closes above/below
    return bars(rows)


def test_sweep_reversal_enters_on_failed_breakdown():
    s = SweepReversal({"lookback": 20, "min_low_age": 4, "qty": 3})
    intent = only(s.on_bars(ctx(sweep_series())), SignalAction.BUY)
    assert intent.qty == Decimal(3)
    assert float(intent.context["swept_low"]) == 95.0
    assert float(intent.context["bar_low"]) < 95.0
    assert float(intent.context["close"]) > 95.0


def test_sweep_reversal_needs_close_back_above():
    """Piercing and staying below is just a breakdown, not a failed one."""
    s = SweepReversal({})
    assert s.on_bars(ctx(sweep_series(close_above=False))) == []


def test_sweep_reversal_requires_an_old_enough_low():
    """Raschke's condition: a low set a day ago has not gathered stops."""
    s = SweepReversal({"min_low_age": 4})
    assert s.on_bars(ctx(sweep_series(low_age=2))) == []
    # The same setup with a sufficiently old low does trigger.
    assert s.on_bars(ctx(sweep_series(low_age=10))) != []


def test_sweep_reversal_needs_an_actual_sweep():
    """No penetration of the prior low means no setup."""
    s = SweepReversal({})
    assert s.on_bars(ctx(bars([(100, 101, 99, 100) for _ in range(31)]))) == []


def test_sweep_reversal_exits_on_stop_and_target():
    s = SweepReversal({"stop_pct": 0.03, "target_pct": 0.06})
    series = bars([(100, 101, 99, 100) for _ in range(31)])

    stopped = only(
        s.on_bars(ctx(series, make_position(qty="1", price="110"))), SignalAction.CLOSE
    )
    assert stopped.context["exit_reason"] == "stop"

    won = only(
        s.on_bars(ctx(series, make_position(qty="1", price="90"))), SignalAction.CLOSE
    )
    assert won.context["exit_reason"] == "target"


def test_sweep_reversal_gives_up_when_thesis_breaks():
    """Closing back under the swept low means the breakdown did not fail."""
    rows = [(100, 101, 99, 100) for _ in range(30)]
    rows[-10] = (100, 101, 95, 100)
    rows.append((97, 98, 93, 93.5))  # closes below the 95 swept low
    s = SweepReversal({"stop_pct": 0.5, "target_pct": 5.0})  # brackets far away
    intent = only(
        s.on_bars(ctx(bars(rows), make_position(qty="1", price="96"))),
        SignalAction.CLOSE,
    )
    assert intent.context["exit_reason"] == "thesis_invalidated"


def test_sweep_reversal_trend_filter():
    downtrend = make_bars(n=260, start_price=300, step=-1.0)
    s = SweepReversal({"trend_sma": 200})
    assert s.on_bars(ctx(downtrend)) == []


@pytest.mark.parametrize(
    "params,match",
    [
        ({"lookback": 3}, "lookback"),
        ({"lookback": 10, "min_low_age": 10}, "min_low_age"),
        ({"stop_pct": 0}, "stop_pct"),
        ({"target_pct": 0}, "target_pct"),
        ({"max_hold_bars": 5}, "max_hold_bars"),
        ({"trend_sma": True}, "trend_sma"),
    ],
)
def test_sweep_reversal_param_validation(params, match):
    with pytest.raises(ValueError, match=match):
        SweepReversal(params)


# ================================================================ dual momentum


def peers(**series) -> dict:
    return {PEERS_KEY: build_peer_features(series)}


def test_dual_momentum_holds_when_both_legs_pass():
    own = make_bars(n=300, start_price=100, step=1.0)  # strong
    weak_peer = make_bars(n=300, start_price=300, step=0.02, symbol="ACWX")
    s = DualMomentum({"peer_symbol": "ACWX", "eval_frequency": "daily", "qty": 4})
    intent = only(s.on_bars(ctx(own, features=peers(ACWX=weak_peer))), SignalAction.BUY)
    assert intent.qty == Decimal(4)
    assert float(intent.context["own_return"]) > float(intent.context["peer_return"])


def test_dual_momentum_relative_leg_blocks_weaker_symbol():
    own = make_bars(n=300, start_price=300, step=0.02)  # barely rising
    strong_peer = make_bars(n=300, start_price=100, step=1.0, symbol="ACWX")
    s = DualMomentum({"peer_symbol": "ACWX", "eval_frequency": "daily"})
    assert s.on_bars(ctx(own, features=peers(ACWX=strong_peer))) == []


def test_dual_momentum_absolute_leg_blocks_falling_symbol():
    falling = make_bars(n=300, start_price=400, step=-1.0)
    weak_peer = make_bars(n=300, start_price=400, step=-2.0, symbol="ACWX")
    # Beats the peer, but its own return is negative: absolute leg fails.
    s = DualMomentum({"peer_symbol": "ACWX", "eval_frequency": "daily"})
    assert s.on_bars(ctx(falling, features=peers(ACWX=weak_peer))) == []


def test_dual_momentum_exit_reports_which_leg_failed():
    falling = make_bars(n=300, start_price=400, step=-1.0)
    s = DualMomentum({"eval_frequency": "daily"})
    intent = only(
        s.on_bars(ctx(falling, make_position(qty="5"))), SignalAction.CLOSE
    )
    assert intent.context["exit_reason"] == "absolute"
    assert intent.qty == Decimal(5)


def test_dual_momentum_flags_missing_peer_data():
    """Without peers this is plain absolute momentum, and must say so."""
    own = make_bars(n=300, start_price=100, step=1.0)
    s = DualMomentum({"peer_symbol": "ACWX", "eval_frequency": "daily"})
    intent = only(s.on_bars(ctx(own)), SignalAction.BUY)
    assert intent.context["peer_skipped"] == "true"
    assert "peer_return" not in intent.context


def test_dual_momentum_can_require_peer_data():
    own = make_bars(n=300, start_price=100, step=1.0)
    s = DualMomentum(
        {"peer_symbol": "ACWX", "require_peer_data": True, "eval_frequency": "daily"}
    )
    assert s.on_bars(ctx(own)) == []


def test_dual_momentum_uses_cash_symbol_as_floor():
    """Antonacci's absolute leg compares against cash, not against zero."""
    own = make_bars(n=300, start_price=100, step=0.05)  # slightly positive
    rich_cash = make_bars(n=300, start_price=100, step=0.5, symbol="BIL")
    s = DualMomentum({"cash_symbol": "BIL", "eval_frequency": "daily"})
    # Own return is positive but below the cash proxy, so no entry.
    assert s.on_bars(ctx(own, features=peers(BIL=rich_cash))) == []


def test_dual_momentum_monthly_cadence():
    own = make_bars(n=300, start_price=100, step=1.0)
    s = DualMomentum({})  # monthly by default
    mid_month = month_start = None
    for i in range(s.warmup_bars(), len(own)):
        if own[i].ts.month == own[i - 1].ts.month and mid_month is None:
            mid_month = i
        if own[i].ts.month != own[i - 1].ts.month and month_start is None:
            month_start = i
    assert s.on_bars(ctx(own[: mid_month + 1])) == []
    assert s.on_bars(ctx(own[: month_start + 1])) != []


@pytest.mark.parametrize(
    "params,match",
    [
        ({"lookback_days": 1}, "lookback_days"),
        ({"lookback_key": "return_99y"}, "lookback_key"),
        ({"peer_symbol": ""}, "peer_symbol"),
        ({"cash_symbol": 5}, "cash_symbol"),
        ({"eval_frequency": "weekly"}, "eval_frequency"),
        ({"qty": 0}, "qty"),
    ],
)
def test_dual_momentum_param_validation(params, match):
    with pytest.raises(ValueError, match=match):
        DualMomentum(params)


# ================================================================ peers plumbing


def test_build_and_read_peer_features():
    series = {
        "SPY": make_bars(n=300, start_price=100, step=0.5, symbol="SPY"),
        "ACWX": make_bars(n=300, start_price=100, step=0.1, symbol="ACWX"),
    }
    features = {PEERS_KEY: build_peer_features(series)}
    spy = read_peer(features, "spy")  # case-insensitive
    acwx = read_peer(features, "ACWX")
    assert spy is not None and acwx is not None
    assert spy.get_return("return_12m") > acwx.get_return("return_12m")
    assert read_peer(features, "NOPE") is None
    assert read_peer({}, "SPY") is None


def test_peer_features_skip_symbols_without_history():
    features = build_peer_features({"SPY": make_bars(n=1, symbol="SPY")})
    assert features == {}


def test_benchmark_carries_trend_flag():
    up = build_benchmark_features("SPY", make_bars(n=300, start_price=100, step=1.0))
    down = build_benchmark_features("SPY", make_bars(n=300, start_price=400, step=-1.0))
    assert up["above_trend"] is True
    assert down["above_trend"] is False


# ================================================================ cross-cutting


ALL = [
    (ONeilBreakout, {}),
    (SweepReversal, {}),
    (DualMomentum, {"eval_frequency": "daily"}),
]


@pytest.mark.parametrize("cls,params", ALL)
def test_short_input_is_safe(cls, params):
    assert cls(params).on_bars(ctx(make_bars(n=3))) == []


@pytest.mark.parametrize("cls,params", ALL)
def test_determinism(cls, params):
    s = cls(params)
    c = ctx(make_bars(n=300, start_price=100, step=0.4), features=bench())
    assert s.on_bars(c) == s.on_bars(c)


@pytest.mark.parametrize("cls,params", ALL)
def test_long_only(cls, params):
    s = cls(params)
    for step in (1.0, -1.0, 0.0):
        series = make_bars(n=300, start_price=200, step=step)
        for position in (None, make_position(qty="5")):
            for intent in s.on_bars(ctx(series, position, features=bench())):
                assert intent.action in (SignalAction.BUY, SignalAction.CLOSE)


def test_all_registered():
    from app.strategies import registry

    registry.load_builtins()
    known = registry.known_strategies()
    for path in (
        "app.strategies.builtin.oneil.ONeilBreakout",
        "app.strategies.builtin.sweep_reversal.SweepReversal",
        "app.strategies.builtin.dual_momentum.DualMomentum",
    ):
        assert path in known
