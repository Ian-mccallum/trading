"""Phase 1 persona tests: Paul Tudor Jones' 200-day rule and Minervini's
Trend Template.

The Minervini tests exercise each of the eight criteria in isolation by
constructing a passing series and then breaking exactly one criterion, so a
regression names the criterion it broke.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.models import SignalAction
from app.schemas.core import BarData
from app.strategies.base import StrategyContext
from app.strategies.benchmark import FEATURE_KEY, build_benchmark_features
from app.strategies.builtin.minervini import MinerviniTrendTemplate
from app.strategies.builtin.ptj_trend import TrendRegime200
from tests.conftest import make_bars, make_position

# ---------------------------------------------------------------- helpers


def bars_from_closes(closes: list[float], symbol: str = "AAPL") -> list[BarData]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        BarData(
            symbol=symbol, timeframe="1Day", ts=start + timedelta(days=i),
            open=Decimal(str(c)), high=Decimal(str(c)),
            low=Decimal(str(c)), close=Decimal(str(c)),
        )
        for i, c in enumerate(closes)
    ]


def ctx(bars, position=None, features=None, symbol="AAPL") -> StrategyContext:
    return StrategyContext(
        symbol=symbol, bars=bars, position=position,
        equity=Decimal(100_000), features=features or {},
    )


def only(intents, action) -> None:
    assert len(intents) == 1, f"expected 1 intent, got {intents}"
    assert intents[0].action == action


# ================================================================ PTJ


def test_ptj_buys_above_200_sma():
    s = TrendRegime200({"qty": 3})
    intents = s.on_bars(ctx(make_bars(n=260, start_price=100, step=1.0)))
    only(intents, SignalAction.BUY)
    assert intents[0].qty == Decimal(3)


def test_ptj_stays_flat_below_200_sma():
    s = TrendRegime200({})
    assert s.on_bars(ctx(make_bars(n=260, start_price=400, step=-1.0))) == []


def test_ptj_exits_below_200_sma():
    s = TrendRegime200({})
    intents = s.on_bars(
        ctx(make_bars(n=260, start_price=400, step=-1.0), make_position(qty="7"))
    )
    only(intents, SignalAction.CLOSE)
    assert intents[0].qty == Decimal(7)
    assert intents[0].context["exit_reason"] == "below_sma"


def test_ptj_holds_while_above():
    s = TrendRegime200({})
    assert s.on_bars(
        ctx(make_bars(n=260, start_price=100, step=1.0), make_position(qty="5"))
    ) == []


def test_ptj_buffer_damps_whipsaw():
    """Just above the SMA, a 5% buffer suppresses the entry."""
    closes = [100.0] * 250 + [101.0]  # barely above a ~100 SMA
    bare = TrendRegime200({})
    buffered = TrendRegime200({"buffer_pct": 0.05})
    assert bare.on_bars(ctx(bars_from_closes(closes))) != []
    assert buffered.on_bars(ctx(bars_from_closes(closes))) == []


def test_ptj_stop_and_target_bracket():
    # Flat-ish series above its SMA so the regime rule itself stays quiet.
    closes = [100.0] * 250 + [102.0]
    s = TrendRegime200({"stop_pct": 0.02, "reward_ratio": 5.0})

    # Entry 120, close 102 -> below the 2% stop (117.6).
    stopped = s.on_bars(ctx(bars_from_closes(closes), make_position(qty="1", price="120")))
    only(stopped, SignalAction.CLOSE)
    assert stopped[0].context["exit_reason"] == "stop"

    # Entry 90, close 102 -> above the 10% target (99.0).
    won = s.on_bars(ctx(bars_from_closes(closes), make_position(qty="1", price="90")))
    only(won, SignalAction.CLOSE)
    assert won[0].context["exit_reason"] == "target"

    # Entry 101 -> inside the bracket, hold.
    assert s.on_bars(ctx(bars_from_closes(closes), make_position(qty="1", price="101"))) == []


def test_ptj_bracket_disabled_by_default():
    closes = [100.0] * 250 + [102.0]
    s = TrendRegime200({})
    assert s.on_bars(ctx(bars_from_closes(closes), make_position(qty="1", price="120"))) == []


def test_ptj_warmup_safety():
    assert TrendRegime200({}).on_bars(ctx(make_bars(n=20))) == []


@pytest.mark.parametrize(
    "params,match",
    [
        ({"sma_period": 1}, "sma_period"),
        ({"qty": 0}, "qty"),
        ({"buffer_pct": 1.0}, "buffer_pct"),
        ({"stop_pct": 0}, "stop_pct"),
        ({"stop_pct": 1.5}, "stop_pct"),
        ({"reward_ratio": 0}, "reward_ratio"),
    ],
)
def test_ptj_param_validation(params, match):
    with pytest.raises(ValueError, match=match):
        TrendRegime200(params)


# ================================================================ Minervini


def passing_series() -> list[float]:
    """A clean Stage 2 uptrend that satisfies all seven price criteria:
    long steady advance, price at its highs, far above the 52-week low."""
    return [100.0 + i * 0.6 for i in range(300)]


def bench_features(step: float = 0.1) -> dict:
    """Benchmark rising more slowly than `passing_series` -> outperformance."""
    return {
        FEATURE_KEY: build_benchmark_features(
            "SPY", make_bars(n=300, start_price=100, step=step, symbol="SPY")
        )
    }


def test_minervini_all_criteria_pass_triggers_buy():
    s = MinerviniTrendTemplate({"qty": 2})
    intents = s.on_bars(ctx(bars_from_closes(passing_series()), features=bench_features()))
    only(intents, SignalAction.BUY)
    assert intents[0].qty == Decimal(2)
    assert intents[0].context["criteria_passed"] == "8"
    assert "failed" not in intents[0].context


def test_minervini_evaluate_reports_all_eight():
    s = MinerviniTrendTemplate({})
    results, _ = s.evaluate(ctx(bars_from_closes(passing_series()), features=bench_features()))
    assert len(results) == 8
    assert all(results.values())


def test_minervini_c1_price_below_slow_ma_blocks():
    # Long uptrend then a crash below the 200-day MA.
    closes = passing_series() + [120.0]
    s = MinerviniTrendTemplate({})
    results, _ = s.evaluate(ctx(bars_from_closes(closes), features=bench_features()))
    assert results["c1_above_mid_and_slow"] is False
    assert s.on_bars(ctx(bars_from_closes(closes), features=bench_features())) == []


def test_minervini_c3_requires_rising_slow_ma():
    # Downtrend: the 200-day MA is falling.
    closes = [400.0 - i * 0.6 for i in range(300)]
    s = MinerviniTrendTemplate({})
    results, _ = s.evaluate(ctx(bars_from_closes(closes)))
    assert results["c3_slow_rising"] is False


def test_minervini_c6_too_close_to_52w_low():
    """Flat series: price sits at its 52-week low, so the 25%-above rule fails."""
    s = MinerviniTrendTemplate({})
    results, _ = s.evaluate(ctx(bars_from_closes([100.0] * 300)))
    assert results["c6_above_52w_low"] is False


def test_minervini_c7_too_far_below_52w_high():
    # Rise then fall 40% off the high while staying above the MAs early on.
    closes = [100.0 + i * 1.0 for i in range(260)] + [200.0] * 40
    s = MinerviniTrendTemplate({})
    results, _ = s.evaluate(ctx(bars_from_closes(closes)))
    assert results["c7_near_52w_high"] is False


def test_minervini_c8_underperformance_blocks_entry():
    """Symbol rising slower than the benchmark fails the RS criterion."""
    s = MinerviniTrendTemplate({})
    slow_symbol = [100.0 + i * 0.05 for i in range(300)]
    strong_bench = bench_features(step=2.0)
    results, detail = s.evaluate(ctx(bars_from_closes(slow_symbol), features=strong_bench))
    assert results["c8_relative_strength"] is False
    assert float(detail["relative_return"]) < 0


def test_minervini_rs_skipped_without_benchmark_is_flagged():
    """Missing benchmark must never silently pass as if it were checked."""
    s = MinerviniTrendTemplate({})
    intents = s.on_bars(ctx(bars_from_closes(passing_series())))  # no features
    only(intents, SignalAction.BUY)
    assert intents[0].context["rs_skipped"] == "true"
    assert "relative_return" not in intents[0].context


def test_minervini_require_rs_data_refuses_without_benchmark():
    s = MinerviniTrendTemplate({"require_rs_data": True})
    results, detail = s.evaluate(ctx(bars_from_closes(passing_series())))
    assert results["c8_relative_strength"] is False
    assert detail["rs_skipped"] == "true"
    assert s.on_bars(ctx(bars_from_closes(passing_series()))) == []


def test_minervini_no_rebuy_while_long():
    s = MinerviniTrendTemplate({})
    assert s.on_bars(
        ctx(bars_from_closes(passing_series()), make_position(qty="5"),
            features=bench_features())
    ) == []


def test_minervini_exits_on_template_fail():
    closes = passing_series() + [120.0]  # crashes out of Stage 2
    s = MinerviniTrendTemplate({})
    intents = s.on_bars(
        ctx(bars_from_closes(closes), make_position(qty="4"), features=bench_features())
    )
    only(intents, SignalAction.CLOSE)
    assert intents[0].qty == Decimal(4)
    assert intents[0].context["exit_reason"] == "template_fail"
    assert "failed" in intents[0].context


def test_minervini_sma_fast_exit_mode():
    """In sma_fast mode the exit is driven solely by criterion 5 (price below
    the 50-day MA), and reports that as its reason."""
    # Price pulls back below the 50-day MA but stays above the 150/200.
    closes = passing_series() + [255.0]
    fast_mode = MinerviniTrendTemplate({"exit_mode": "sma_fast"})
    results, _ = fast_mode.evaluate(ctx(bars_from_closes(closes), features=bench_features()))
    assert results["c5_above_fast"] is False
    intents = fast_mode.on_bars(
        ctx(bars_from_closes(closes), make_position(qty="1"), features=bench_features())
    )
    only(intents, SignalAction.CLOSE)
    assert intents[0].context["exit_reason"] == "below_sma_fast"


def test_minervini_book_variant_30_percent_threshold():
    """low_multiple 1.30 is the Trade Like a Stock Market Wizard variant."""
    closes = [100.0] * 250 + [128.0] * 50  # 28% above the 52-week low
    default = MinerviniTrendTemplate({})
    strict = MinerviniTrendTemplate({"low_multiple": 1.30})
    assert default.evaluate(ctx(bars_from_closes(closes)))[0]["c6_above_52w_low"] is True
    assert strict.evaluate(ctx(bars_from_closes(closes)))[0]["c6_above_52w_low"] is False


def test_minervini_warmup_safety():
    s = MinerviniTrendTemplate({})
    assert s.on_bars(ctx(make_bars(n=100))) == []
    assert s.evaluate(ctx(make_bars(n=100))) is None


def test_minervini_determinism():
    s = MinerviniTrendTemplate({})
    c = ctx(bars_from_closes(passing_series()), features=bench_features())
    assert s.on_bars(c) == s.on_bars(c)


@pytest.mark.parametrize(
    "params,match",
    [
        ({"sma_fast": 200, "sma_mid": 150}, "sma_fast < sma_mid"),
        ({"exit_mode": "trailing"}, "exit_mode"),
        ({"slow_rising_days": 0}, "slow_rising_days"),
        ({"low_multiple": 0.9}, "low_multiple"),
        ({"high_ratio": 1.5}, "high_ratio"),
        ({"rs_lookback": 12}, "rs_lookback"),
        ({"qty": -1}, "qty"),
    ],
)
def test_minervini_param_validation(params, match):
    with pytest.raises(ValueError, match=match):
        MinerviniTrendTemplate(params)


# ================================================================ cross-cutting


@pytest.mark.parametrize("cls", [TrendRegime200, MinerviniTrendTemplate])
def test_personas_are_long_only(cls):
    """Neither persona may ever emit a SELL from any context."""
    s = cls({})
    for step in (1.0, -1.0, 0.0):
        bars = make_bars(n=320, start_price=200, step=step)
        for position in (None, make_position(qty="5")):
            for intent in s.on_bars(ctx(bars, position, features=bench_features())):
                assert intent.action in (SignalAction.BUY, SignalAction.CLOSE)


def test_personas_registered():
    from app.strategies import registry

    registry.load_builtins()
    known = registry.known_strategies()
    assert "app.strategies.builtin.ptj_trend.TrendRegime200" in known
    assert "app.strategies.builtin.minervini.MinerviniTrendTemplate" in known


def test_bar_lookback_covers_every_strategy_warmup():
    """Regression: BAR_LOOKBACK was 200 while Minervini needs 253, so the
    worker silently skipped it — strategies never ran and nothing traded."""
    from app.strategies import registry
    from app.workers.tasks import BAR_LOOKBACK

    registry.load_builtins()
    for path, cls in registry.known_strategies().items():
        warmup = cls({"target_weight": 0.25} if "Sleeve" in path else {}).warmup_bars()
        assert warmup <= BAR_LOOKBACK, f"{path} needs {warmup} bars > BAR_LOOKBACK {BAR_LOOKBACK}"
