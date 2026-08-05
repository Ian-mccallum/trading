"""Phase 4 tests: owner profile and position sizing.

The load-bearing property is that the profile can only ever be *more*
conservative than the risk engine. It shapes what gets proposed; it never
touches evaluation. These tests pin the three rules that keep that true:
entries only, self-sizing strategies untouched, exits never rewritten.
"""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path

import pytest

from app.db.models import SignalAction
from app.profile.model import (
    RISK_MULTIPLIERS,
    PersonaWeight,
    Profile,
    load_profile,
    parse_profile,
)
from app.profile.sizing import cap_open_positions, size_intent
from app.schemas.core import TradeIntent

# ---------------------------------------------------------------- helpers


def intent(symbol="SPY", action=SignalAction.BUY, qty="1") -> TradeIntent:
    return TradeIntent(symbol=symbol, action=action, qty=Decimal(qty))


def profile(**overrides) -> Profile:
    base = {
        "name": "test",
        "capital_allocation": Decimal(25_000),
        "risk_appetite": "moderate",
        "max_positions": 5,
    }
    base.update(overrides)
    return Profile(**base)


# ---------------------------------------------------------------- model


def test_per_position_target_math():
    # 25,000 / 5 positions * 1.0 (moderate) = 5,000
    assert profile().per_position_target() == Decimal(5_000)
    assert profile(risk_appetite="conservative").per_position_target() == Decimal(2_500)
    assert profile(risk_appetite="aggressive").per_position_target() == Decimal(7_500)


def test_risk_multipliers_cannot_leverage():
    """Even the widest appetite is a preference dial, not leverage."""
    assert max(RISK_MULTIPLIERS.values()) <= Decimal("1.5")


def test_persona_weight_defaults_to_one():
    """An unlisted persona must not be silently disabled."""
    p = profile(personas=(PersonaWeight("minervini", Decimal("0.5")),))
    assert p.persona_weight("minervini") == Decimal("0.5")
    assert p.persona_weight("unlisted") == Decimal(1)
    assert p.persona_weight("") == Decimal(1)


def test_allows_symbol_rules():
    empty = profile()
    assert empty.allows_symbol("ANY")  # no list = anything the engine permits

    listed = profile(symbols=("SPY", "QQQ"))
    assert listed.allows_symbol("spy")  # case-insensitive
    assert not listed.allows_symbol("TSLA")

    excluded = profile(exclusions=frozenset({"TSLA"}))
    assert not excluded.allows_symbol("tsla")
    assert excluded.allows_symbol("SPY")


# ---------------------------------------------------------------- parsing


def test_parse_full_profile():
    p = parse_profile(
        {
            "name": "ian",
            "capital_allocation": 50_000,
            "risk_appetite": "aggressive",
            "max_positions": 4,
            "symbols": ["spy", " qqq "],
            "exclusions": ["tsla"],
            "benchmark": "vti",
            "max_position_fraction": 0.5,
            "min_order_notional": 250,
            "personas": [{"name": "minervini", "weight": 0.8}],
        }
    )
    assert p.name == "ian"
    assert p.symbols == ("SPY", "QQQ")  # normalized and trimmed
    assert p.exclusions == frozenset({"TSLA"})
    assert p.benchmark == "VTI"
    assert p.personas[0].weight == Decimal("0.8")
    assert p.per_position_target() == Decimal("18750.0")  # 50000/4*1.5


def test_parse_empty_gives_defaults():
    p = parse_profile({})
    assert p.name == "default"
    assert p.capital_allocation == Decimal(25_000)
    assert p.risk_appetite == "moderate"


@pytest.mark.parametrize(
    "data,match",
    [
        ({"capital_allocation": 0}, "capital_allocation"),
        ({"capital_allocation": -5}, "capital_allocation"),
        ({"capital_allocation": "abc"}, "not a valid number"),
        ({"risk_appetite": "reckless"}, "risk_appetite"),
        ({"max_positions": 0}, "max_positions"),
        ({"max_positions": True}, "max_positions"),
        ({"symbols": "SPY"}, "symbols"),
        ({"symbols": [1, 2]}, "symbols"),
        ({"max_position_fraction": 1.5}, "max_position_fraction"),
        ({"min_order_notional": -1}, "min_order_notional"),
        ({"personas": [{"weight": 1}]}, "needs a 'name'"),
        ({"personas": [{"name": "x", "weight": 0}]}, "weight"),
        ({"personas": [{"name": "x", "weight": 5}]}, "weight"),
        ({"personas": "minervini"}, "personas"),
        ({"benchmark": "  "}, "benchmark"),
    ],
)
def test_parse_rejects_bad_config(data, match):
    """A typo here changes what the bot trades, so it must fail loudly."""
    with pytest.raises(ValueError, match=match):
        parse_profile(data)


def test_symbols_and_exclusions_cannot_overlap():
    """A symbol that is both tradeable and excluded is a config bug, and
    guessing which the operator meant would be worse than refusing."""
    with pytest.raises(ValueError, match="overlap"):
        parse_profile({"symbols": ["SPY"], "exclusions": ["spy"]})


def test_load_missing_profile_returns_defaults(tmp_path):
    assert load_profile(tmp_path / "nope.toml") == Profile()


def test_load_real_file(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text(
        'name = "loaded"\ncapital_allocation = 10000\nmax_positions = 2\n'
    )
    p = load_profile(path)
    assert p.name == "loaded"
    assert p.per_position_target() == Decimal(5_000)


def test_shipped_example_profile_is_valid():
    """The example must parse: it is the template every operator copies."""
    path = Path(__file__).resolve().parents[1] / "profile.example.toml"
    with path.open("rb") as handle:
        parsed = parse_profile(tomllib.load(handle))
    assert parsed.name == "ian"
    assert parsed.capital_allocation == Decimal(25_000)
    assert len(parsed.personas) == 3


# ---------------------------------------------------------------- sizing


def test_sizes_entry_to_per_position_target():
    # 5,000 budget / $500 price = 10 shares.
    result = size_intent(intent(), profile(), Decimal(500))
    assert result.intent.qty == Decimal(10)
    assert result.intent.context["sized_by"] == "profile"
    assert result.intent.context["strategy_qty"] == "1"


def test_sizing_rounds_down_to_whole_shares():
    # 5,000 / 740.80 = 6.75 -> 6
    assert size_intent(intent(), profile(), Decimal("740.80")).intent.qty == Decimal(6)


def test_persona_weight_scales_size():
    p = profile(personas=(PersonaWeight("minervini", Decimal("0.5")),))
    full = size_intent(intent(), p, Decimal(100), persona="other").intent.qty
    half = size_intent(intent(), p, Decimal(100), persona="minervini").intent.qty
    assert full == Decimal(50)
    assert half == Decimal(25)


def test_max_position_fraction_caps_size():
    """Even a huge persona weight cannot exceed the per-position ceiling."""
    p = profile(
        max_position_fraction=Decimal("0.10"),  # 2,500 of 25,000
        personas=(PersonaWeight("big", Decimal(2)),),
    )
    result = size_intent(intent(), p, Decimal(100), persona="big")
    assert result.intent.qty == Decimal(25)  # capped at 2,500, not 10,000


def test_exits_are_never_resized():
    """A CLOSE quantity comes from the real position; rewriting it could try
    to sell shares that are not held."""
    exit_intent = intent(action=SignalAction.CLOSE, qty="37")
    result = size_intent(exit_intent, profile(), Decimal(500))
    assert result.intent.qty == Decimal(37)
    assert "sized_by" not in result.intent.context

    sell = intent(action=SignalAction.SELL, qty="12")
    assert size_intent(sell, profile(), Decimal(500)).intent.qty == Decimal(12)


def test_self_sizing_strategies_are_untouched():
    """A sleeve has already computed its quantity from its target weight."""
    result = size_intent(intent(qty="250"), profile(), Decimal(100), self_sized=True)
    assert result.intent.qty == Decimal(250)
    assert result.reason == "strategy sizes itself"


def test_excluded_symbol_is_dropped():
    p = profile(exclusions=frozenset({"TSLA"}))
    result = size_intent(intent(symbol="TSLA"), p, Decimal(100))
    assert result.dropped
    assert "not tradeable" in result.reason


def test_symbol_outside_profile_list_is_dropped():
    p = profile(symbols=("SPY",))
    assert size_intent(intent(symbol="QQQ"), p, Decimal(100)).dropped


def test_unaffordable_single_share_is_dropped():
    """One share above the whole per-position budget cannot be sized down."""
    p = profile(capital_allocation=Decimal(1_000), max_positions=5)  # 200 budget
    result = size_intent(intent(), p, Decimal(5_000))
    assert result.dropped
    assert "exceeds the per-position budget" in result.reason


def test_dust_orders_are_dropped():
    p = profile(capital_allocation=Decimal(1_000), max_positions=5, min_order_notional=Decimal(500))
    # Budget 200 buys 2 shares at 100 = 200 notional, below the 500 floor.
    result = size_intent(intent(), p, Decimal(100))
    assert result.dropped
    assert "minimum" in result.reason


def test_zero_price_is_dropped():
    assert size_intent(intent(), profile(), Decimal(0)).dropped


def test_sizing_preserves_existing_context():
    original = TradeIntent(
        symbol="SPY", action=SignalAction.BUY, qty=Decimal(1),
        context={"pivot": "100.0"},
    )
    result = size_intent(original, profile(), Decimal(500))
    assert result.intent.context["pivot"] == "100.0"
    assert result.intent.context["sized_by"] == "profile"


# ---------------------------------------------------------------- position cap


def test_cap_blocks_new_positions_beyond_limit():
    p = profile(max_positions=2)
    intents = [intent(symbol=s) for s in ("SPY", "QQQ", "AAPL")]
    kept, dropped = cap_open_positions(intents, p, open_symbols=set())
    assert [i.symbol for i in kept] == ["SPY", "QQQ"]
    assert len(dropped) == 1
    assert "AAPL" in dropped[0]


def test_cap_counts_existing_positions():
    p = profile(max_positions=2)
    kept, dropped = cap_open_positions([intent(symbol="AAPL")], p, {"SPY", "QQQ"})
    assert kept == []
    assert len(dropped) == 1


def test_cap_allows_adding_to_an_existing_position():
    """Already holding it means no new position is being opened."""
    p = profile(max_positions=1)
    kept, dropped = cap_open_positions([intent(symbol="SPY")], p, {"SPY"})
    assert len(kept) == 1
    assert dropped == []


def test_cap_never_blocks_an_exit():
    """Refusing to close a position because of a count limit would be
    actively harmful."""
    p = profile(max_positions=1)
    exits = [
        intent(symbol="AAPL", action=SignalAction.CLOSE, qty="5"),
        intent(symbol="MSFT", action=SignalAction.SELL, qty="3"),
    ]
    kept, dropped = cap_open_positions(exits, p, {"SPY"})
    assert len(kept) == 2
    assert dropped == []


# ---------------------------------------------------------------- comparison helpers


def test_buy_and_hold_uses_the_same_capital_as_strategies():
    """The reference must be sized like every other row, or the comparison
    silently flatters strategies that deploy less capital."""
    from datetime import UTC, datetime, timedelta

    from app.schemas.core import BarData
    from scripts.compare_personas import buy_and_hold

    start = datetime(2024, 1, 1, tzinfo=UTC)
    series = [
        BarData(
            symbol="SPY", timeframe="1Day", ts=start + timedelta(days=i),
            open=Decimal(100), high=Decimal(100), low=Decimal(100), close=Decimal(price),
        )
        for i, price in enumerate([100, 200])
    ]
    # $5,000 buys 50 shares at 100; +100 each = $5,000 on 100k capital = 5%.
    assert buy_and_hold(series, Decimal(5_000)) == pytest.approx(0.05)


def test_buy_and_hold_handles_degenerate_input():
    from scripts.compare_personas import buy_and_hold

    assert buy_and_hold([], Decimal(5_000)) == 0.0


def test_sized_params_scales_qty_to_the_position_budget():
    """Comparing one $740 share against $5,000 of the same symbol measures
    position size, not skill."""
    from app.db.models import Strategy as StrategyRow
    from scripts.compare_personas import sized_params

    row = StrategyRow(name="x", version=1, class_path="x.Y", params={"qty": 1, "fast": 5})
    sized = sized_params(row, price=500.0, per_position=Decimal(5_000))
    assert sized["qty"] == 10
    assert sized["fast"] == 5  # other params untouched


def test_sized_params_leaves_self_sizing_strategies_alone():
    """A sleeve has no qty param; there is nothing to override."""
    from app.db.models import Strategy as StrategyRow
    from scripts.compare_personas import sized_params

    row = StrategyRow(
        name="leg", version=1, class_path="x.Y", params={"target_weight": 0.25}
    )
    assert sized_params(row, 500.0, Decimal(5_000)) == {"target_weight": 0.25}


def test_sized_params_never_returns_zero_shares():
    from app.db.models import Strategy as StrategyRow
    from scripts.compare_personas import sized_params

    row = StrategyRow(name="x", version=1, class_path="x.Y", params={"qty": 1})
    assert sized_params(row, price=99_999.0, per_position=Decimal(5_000))["qty"] == 1


# ---------------------------------------------------------------- capital base


def test_sleeve_sizes_off_allocated_capital_not_account_equity():
    """A 25% leg means a quarter of the money the bot was GIVEN.

    Sizing off full account equity was the real reason portfolio personas
    could not trade: a 25% leg of a $100k account is $25,000, far above any
    sane per-order limit, even when the owner only allocated $25k.
    """
    from datetime import UTC, datetime

    from app.schemas.core import BarData
    from app.strategies.base import StrategyContext
    from app.strategies.builtin.target_weight_sleeve import TargetWeightSleeve

    bars = [
        BarData(
            symbol="VTI", timeframe="1Day", ts=datetime(2026, 1, 1, tzinfo=UTC),
            open=Decimal(100), high=Decimal(100), low=Decimal(100), close=Decimal(100),
        )
    ]
    sleeve = TargetWeightSleeve({"target_weight": 0.25})

    allocated = StrategyContext(
        symbol="VTI", bars=bars, position=None,
        equity=Decimal(100_000), investable=Decimal(25_000),
    )
    assert sleeve.on_bars(allocated)[0].qty == Decimal(62)  # 25% of 25k / 100

    # Without a profile it falls back to equity, which is right for backtests.
    whole_account = StrategyContext(
        symbol="VTI", bars=bars, position=None, equity=Decimal(100_000)
    )
    assert whole_account.capital_base == Decimal(100_000)
    assert sleeve.on_bars(whole_account)[0].qty == Decimal(250)


def test_risk_limits_accommodate_the_default_profile():
    """The shipped risk limits must be able to pass the largest leg any
    shipped persona wants, or portfolio personas are structurally dead."""
    from app.config import Settings

    settings = Settings(_env_file=None)
    largest_leg = Profile().capital_allocation * Decimal("0.40")  # All Weather TLT
    assert Decimal(str(settings.risk_max_order_notional)) >= largest_leg
    assert Decimal(str(settings.risk_max_position_notional)) >= largest_leg
    assert Decimal(str(settings.risk_max_gross_exposure)) >= Profile().capital_allocation
