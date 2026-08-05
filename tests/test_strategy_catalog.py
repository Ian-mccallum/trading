"""Tests for the researched quant strategy catalog: TSMOM, Donchian/Turtle,
Connors RSI-2, Double 7s, Bollinger reversion, 52-week high.

Each strategy is tested for: entry on a constructed canonical setup, refusal
without its filter/flat condition, exit behavior, warmup safety, determinism,
and param validation. Bar series are hand-built so expected signals are
verifiable by inspection.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.models import SignalAction
from app.schemas.core import BarData
from app.strategies.base import StrategyContext
from app.strategies.builtin.bollinger_reversion import BollingerReversion
from app.strategies.builtin.connors_rsi2 import ConnorsRsi2
from app.strategies.builtin.donchian_breakout import DonchianBreakout
from app.strategies.builtin.double7 import ConnorsDouble7
from app.strategies.builtin.high_52w import FiftyTwoWeekHigh
from app.strategies.builtin.tsmom import TimeSeriesMomentum
from tests.conftest import make_bars, make_position


def bars_from_closes(closes: list[float], spread: float = 0.5) -> list[BarData]:
    """Bars where high/low hug the close by ±spread (open = close)."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        BarData(
            symbol="SPY", timeframe="1Day", ts=start + timedelta(days=i),
            open=Decimal(str(c)), high=Decimal(str(c + spread)),
            low=Decimal(str(c - spread)), close=Decimal(str(c)),
        )
        for i, c in enumerate(closes)
    ]


def ctx_for(bars, position=None) -> StrategyContext:
    return StrategyContext(
        symbol="SPY", bars=bars, position=position,
        equity=Decimal(100_000), features={},
    )


def single_buy(intents) -> None:
    assert len(intents) == 1
    assert intents[0].action == SignalAction.BUY


def single_close(intents) -> None:
    assert len(intents) == 1
    assert intents[0].action == SignalAction.CLOSE


# ---------------------------------------------------------------- TSMOM


def month_spanning_bars(closes: list[float]) -> list[BarData]:
    """Daily bars long enough to cross month boundaries (calendar days)."""
    return bars_from_closes(closes)


def test_tsmom_daily_entry_on_positive_trailing_return():
    bars = bars_from_closes([100.0] * 100 + [100.0 + i * 0.5 for i in range(160)])
    s = TimeSeriesMomentum({"eval_frequency": "daily", "qty": 3})
    intents = s.on_bars(ctx_for(bars))
    single_buy(intents)
    assert intents[0].qty == Decimal(3)
    assert float(intents[0].context["trailing_return"]) > 0


def test_tsmom_daily_close_on_negative_trailing_return():
    bars = bars_from_closes([200.0 - i * 0.3 for i in range(260)])
    s = TimeSeriesMomentum({"eval_frequency": "daily"})
    intents = s.on_bars(ctx_for(bars, make_position(qty="5")))
    single_close(intents)
    assert intents[0].qty == Decimal(5)


def test_tsmom_no_action_while_long_and_positive():
    bars = bars_from_closes([100.0 + i * 0.5 for i in range(260)])
    s = TimeSeriesMomentum({"eval_frequency": "daily"})
    assert s.on_bars(ctx_for(bars, make_position(qty="5"))) == []


def test_tsmom_monthly_cadence_gates_to_month_start():
    closes = [100.0 + i * 0.5 for i in range(300)]
    bars = month_spanning_bars(closes)
    s = TimeSeriesMomentum({})
    # Find a mid-month bar index and a month-start bar index past warmup.
    mid_month = month_start = None
    for i in range(s.warmup_bars(), len(bars)):
        if bars[i].ts.month == bars[i - 1].ts.month and mid_month is None:
            mid_month = i
        if bars[i].ts.month != bars[i - 1].ts.month and month_start is None:
            month_start = i
    assert s.on_bars(ctx_for(bars[: mid_month + 1])) == []  # silent mid-month
    single_buy(s.on_bars(ctx_for(bars[: month_start + 1])))  # acts at month start


def test_tsmom_vol_targeting_shrinks_qty():
    calm = [100.0 + i * 0.5 for i in range(260)]
    s = TimeSeriesMomentum(
        {"eval_frequency": "daily", "qty": 100, "vol_target_annual": 0.0001}
    )
    intents = s.on_bars(ctx_for(bars_from_closes(calm)))
    single_buy(intents)
    assert intents[0].qty < Decimal(100)  # tiny target vs real vol -> shrunk
    assert intents[0].qty >= Decimal(1)


def test_tsmom_param_validation():
    with pytest.raises(ValueError, match="eval_frequency"):
        TimeSeriesMomentum({"eval_frequency": "hourly"})
    with pytest.raises(ValueError, match="lookback_days"):
        TimeSeriesMomentum({"lookback_days": 5, "skip_days": 10})
    with pytest.raises(ValueError, match="qty"):
        TimeSeriesMomentum({"qty": 0})


def test_tsmom_warmup_safety():
    assert TimeSeriesMomentum({}).on_bars(ctx_for(make_bars(n=10))) == []


# ---------------------------------------------------------------- Donchian


def breakout_bars(n_flat: int = 30, breakout_to: float = 120.0) -> list[BarData]:
    closes = [100.0] * n_flat + [breakout_to]
    return bars_from_closes(closes)


def test_donchian_entry_on_breakout_above_prior_channel():
    s = DonchianBreakout({"entry_days": 20, "exit_days": 10})
    intents = s.on_bars(ctx_for(breakout_bars()))
    single_buy(intents)
    # Channel high excludes the breakout bar: 100 + 0.5 spread.
    assert float(intents[0].context["channel_high"]) == 100.5


def test_donchian_no_lookahead_flat_series_never_breaks_out():
    # Every bar equals the channel high of the preceding window -> no entry.
    s = DonchianBreakout({})
    assert s.on_bars(ctx_for(bars_from_closes([100.0] * 60))) == []


def test_donchian_exit_on_channel_low():
    closes = [100.0] * 30 + [80.0]  # crash through the 10-day low (99.5)
    s = DonchianBreakout({})
    intents = s.on_bars(ctx_for(bars_from_closes(closes), make_position(qty="4", price="110")))
    single_close(intents)
    assert intents[0].context["exit_reason"] == "channel_low"
    assert intents[0].qty == Decimal(4)


def test_donchian_atr_stop_fires_before_channel_low():
    # With +-0.5 bar spreads on 1.0 steps, TR = 1.5 -> ATR ~ 1.5 and the
    # 2N stop from entry 150 sits near 147.0. Close 146.5 pierces the stop
    # while remaining far above the 10-day channel low (~140.5).
    closes = [100.0 + i for i in range(50)] + [148.4, 146.5]
    s = DonchianBreakout({})
    intents = s.on_bars(
        ctx_for(bars_from_closes(closes), make_position(qty="2", price="150"))
    )
    single_close(intents)
    assert intents[0].context["exit_reason"] == "atr_stop"


def test_donchian_atr_stop_disabled_by_null():
    closes = [100.0 + i for i in range(50)] + [148.4, 146.5]
    s = DonchianBreakout({"stop_atr_mult": None})
    assert s.on_bars(
        ctx_for(bars_from_closes(closes), make_position(qty="2", price="150"))
    ) == []


def test_donchian_chandelier_exit():
    # Rise to 149, then fade for 8 bars to 128 — above both entry-relative
    # stop (nullified) and above 10-day channel low? Channel low over last 10
    # excludes current: lows around fading closes... choose fade shallow per
    # bar but deep cumulatively: chandelier = 22-bar high - 3*ATR.
    closes = [100.0 + i for i in range(50)]  # high ~149.5
    closes += [146.0, 143.0, 140.0, 137.0, 134.0]
    s = DonchianBreakout(
        {"stop_atr_mult": None, "chandelier_mult": 3.0, "exit_days": 3}
    )
    intents = s.on_bars(
        ctx_for(bars_from_closes(closes), make_position(qty="1", price="149"))
    )
    single_close(intents)
    assert intents[0].context["exit_reason"] in ("chandelier", "channel_low")


def test_donchian_turtle_s2_preset_needs_55_days():
    s = DonchianBreakout({"entry_days": 55, "exit_days": 20})
    assert s.warmup_bars() >= 56
    closes = [100.0] * 60 + [130.0]
    single_buy(s.on_bars(ctx_for(bars_from_closes(closes))))


def test_donchian_param_validation():
    with pytest.raises(ValueError, match="entry_days"):
        DonchianBreakout({"entry_days": 1})
    with pytest.raises(ValueError, match="stop_atr_mult"):
        DonchianBreakout({"stop_atr_mult": -1})
    with pytest.raises(ValueError, match="qty"):
        DonchianBreakout({"qty": -5})


# ---------------------------------------------------------------- Connors RSI-2


def rsi2_setup_bars() -> list[BarData]:
    """Long uptrend (close > SMA200) with a sharp 3-day pullback at the end
    (RSI(2) pinned near 0), while staying above the 200-SMA."""
    closes = [100.0 + i * 0.5 for i in range(250)]  # ends ~224.5, sma200 ~ 199
    closes += [222.0, 219.5, 217.0]  # three straight down closes, still >> SMA200
    return bars_from_closes(closes)


def test_connors_rsi2_entry_on_pullback_in_uptrend():
    s = ConnorsRsi2({})
    intents = s.on_bars(ctx_for(rsi2_setup_bars()))
    single_buy(intents)
    assert float(intents[0].context["rsi"]) < 10


def test_connors_rsi2_trend_gate_blocks_downtrend():
    closes = [300.0 - i * 0.5 for i in range(250)]  # below SMA200 at the end
    s = ConnorsRsi2({})
    assert s.on_bars(ctx_for(bars_from_closes(closes))) == []


def test_connors_rsi2_no_entry_when_not_oversold():
    closes = [100.0 + i * 0.5 for i in range(250)]  # straight up: RSI(2) = 100
    s = ConnorsRsi2({})
    assert s.on_bars(ctx_for(bars_from_closes(closes))) == []


def test_connors_rsi2_sma_exit():
    # Long, price snaps back above the 5-day SMA -> exit.
    closes = [100.0 + i * 0.5 for i in range(250)] + [220.0, 218.0, 216.0, 230.0]
    s = ConnorsRsi2({})
    intents = s.on_bars(ctx_for(bars_from_closes(closes), make_position(qty="3")))
    single_close(intents)


def test_connors_rsi2_rsi_exit_mode():
    closes = [100.0 + i * 0.5 for i in range(250)]  # RSI(2)=100 > 70
    s = ConnorsRsi2({"exit_mode": "rsi"})
    intents = s.on_bars(ctx_for(bars_from_closes(closes), make_position(qty="3")))
    single_close(intents)


def test_connors_rsi2_param_validation():
    with pytest.raises(ValueError, match="exit_mode"):
        ConnorsRsi2({"exit_mode": "stop"})
    with pytest.raises(ValueError, match="entry_threshold"):
        ConnorsRsi2({"entry_threshold": 60})
    with pytest.raises(ValueError, match="exit_rsi"):
        ConnorsRsi2({"exit_rsi": 40})


# ---------------------------------------------------------------- Double 7s


def test_double7_entry_on_seven_day_low_in_uptrend():
    closes = [100.0 + i * 0.5 for i in range(250)]
    closes += [223.0, 222.0, 221.0, 220.0, 219.0, 218.0, 217.0]  # 7-day closing low
    s = ConnorsDouble7({})
    intents = s.on_bars(ctx_for(bars_from_closes(closes)))
    single_buy(intents)


def test_double7_no_entry_below_trend_sma():
    closes = [300.0 - i * 0.5 for i in range(257)]
    s = ConnorsDouble7({})
    assert s.on_bars(ctx_for(bars_from_closes(closes))) == []


def test_double7_exit_on_seven_day_high_only():
    up = [100.0 + i * 0.5 for i in range(250)]
    position = make_position(qty="2")
    s = ConnorsDouble7({})
    # Rising close = 7-day closing high -> exit fires.
    single_close(s.on_bars(ctx_for(bars_from_closes(up), position)))
    # While long and making new lows, NO re-entry and no exit (precedence).
    lows = up + [220.0, 219.0, 218.0]
    assert s.on_bars(ctx_for(bars_from_closes(lows), position)) == []


def test_double7_param_validation():
    with pytest.raises(ValueError, match="entry_lookback"):
        ConnorsDouble7({"entry_lookback": 1})


# ---------------------------------------------------------------- Bollinger


def bollinger_setup_bars() -> list[BarData]:
    """Uptrend, then a plunge through the lower band on the final bar."""
    closes = [100.0 + i * 0.2 for i in range(240)]
    # Oscillate to give the bands width, then plunge.
    closes += [148.0, 149.5, 148.5, 150.0, 148.8, 149.8, 148.2, 149.9, 148.6, 149.7]
    closes += [139.0]  # far below SMA20 - 2*sigma, still above SMA200 (~124)
    return bars_from_closes(closes)


def test_bollinger_entry_below_lower_band_in_uptrend():
    s = BollingerReversion({})
    intents = s.on_bars(ctx_for(bollinger_setup_bars()))
    single_buy(intents)
    assert float(intents[0].context["close"]) < float(intents[0].context["lower"])


def test_bollinger_trend_gate_blocks_falling_knife():
    closes = [300.0 - i * 0.5 for i in range(250)] + [140.0]
    s = BollingerReversion({})
    assert s.on_bars(ctx_for(bars_from_closes(closes))) == []


def test_bollinger_trend_gate_can_be_disabled():
    closes = [300.0 - i * 0.5 for i in range(250)] + [140.0]
    s = BollingerReversion({"trend_sma": None})
    single_buy(s.on_bars(ctx_for(bars_from_closes(closes))))


def test_bollinger_exit_at_middle_band():
    bars = bars_from_closes([100.0] * 30)  # flat: close == middle band
    s = BollingerReversion({"trend_sma": None})
    intents = s.on_bars(ctx_for(bars, make_position(qty="2")))
    single_close(intents)


def test_bollinger_min_bandwidth_floor():
    # Nearly-flat series then small dip: bands are razor thin.
    closes = [100.0, 100.01] * 15 + [99.9]
    s = BollingerReversion({"trend_sma": None, "min_bandwidth": 0.05})
    assert s.on_bars(ctx_for(bars_from_closes(closes))) == []


def test_bollinger_param_validation():
    with pytest.raises(ValueError, match="num_std"):
        BollingerReversion({"num_std": 0})
    with pytest.raises(ValueError, match="trend_sma"):
        BollingerReversion({"trend_sma": True})


# ---------------------------------------------------------------- 52-week high


def test_high52w_entry_near_high():
    closes = [100.0 + i * 0.2 for i in range(300)]  # closes at its high
    s = FiftyTwoWeekHigh({})
    intents = s.on_bars(ctx_for(bars_from_closes(closes)))
    single_buy(intents)
    assert float(intents[0].context["ratio"]) >= 0.95


def test_high52w_no_entry_far_from_high():
    closes = [200.0] * 260 + [170.0]  # ratio ~0.85: between the bands -> nothing
    s = FiftyTwoWeekHigh({})
    assert s.on_bars(ctx_for(bars_from_closes(closes))) == []


def test_high52w_hysteresis_exit():
    closes = [200.0] * 260 + [155.0]  # ratio ~0.77 <= 0.80
    s = FiftyTwoWeekHigh({})
    intents = s.on_bars(ctx_for(bars_from_closes(closes), make_position(qty="6")))
    single_close(intents)
    assert intents[0].qty == Decimal(6)


def test_high52w_between_bands_holds_position():
    closes = [200.0] * 260 + [170.0]  # ratio ~0.85: no exit while long
    s = FiftyTwoWeekHigh({})
    assert s.on_bars(ctx_for(bars_from_closes(closes), make_position(qty="6"))) == []


def test_high52w_close_basis_variant():
    s = FiftyTwoWeekHigh({"high_basis": "close"})
    closes = [100.0 + i * 0.2 for i in range(300)]
    intents = s.on_bars(ctx_for(bars_from_closes(closes)))
    single_buy(intents)
    assert float(intents[0].context["ratio"]) == 1.0  # max-close == current close


def test_high52w_param_validation():
    with pytest.raises(ValueError, match="exit_ratio < entry_ratio"):
        FiftyTwoWeekHigh({"entry_ratio": 0.8, "exit_ratio": 0.9})
    with pytest.raises(ValueError, match="high_basis"):
        FiftyTwoWeekHigh({"high_basis": "vwap"})


# ---------------------------------------------------------------- cross-cutting


ALL_STRATEGIES = [
    (TimeSeriesMomentum, {"eval_frequency": "daily"}),
    (DonchianBreakout, {}),
    (ConnorsRsi2, {}),
    (ConnorsDouble7, {}),
    (BollingerReversion, {}),
    (FiftyTwoWeekHigh, {}),
]


@pytest.mark.parametrize("cls,params", ALL_STRATEGIES)
def test_short_input_is_safe(cls, params):
    assert cls(params).on_bars(ctx_for(make_bars(n=3))) == []


@pytest.mark.parametrize("cls,params", ALL_STRATEGIES)
def test_determinism(cls, params):
    bars = make_bars(n=300, start_price=100, step=0.4)
    ctx = ctx_for(bars)
    s = cls(params)
    assert s.on_bars(ctx) == s.on_bars(ctx)


@pytest.mark.parametrize("cls,params", ALL_STRATEGIES)
def test_long_only_never_sells_short(cls, params):
    """No strategy may emit SELL (only BUY/CLOSE) from any of these contexts."""
    for step in (0.5, -0.5, 0.0):
        bars = make_bars(n=300, start_price=200, step=step)
        for position in (None, make_position(qty="5")):
            for intent in cls(params).on_bars(ctx_for(bars, position)):
                assert intent.action in (SignalAction.BUY, SignalAction.CLOSE)


def test_all_registered_with_expected_names():
    from app.strategies import registry

    registry.load_builtins()
    known = registry.known_strategies()
    for cls in (TimeSeriesMomentum, DonchianBreakout, ConnorsRsi2,
                ConnorsDouble7, BollingerReversion, FiftyTwoWeekHigh):
        assert f"{cls.__module__}.{cls.__name__}" in known
