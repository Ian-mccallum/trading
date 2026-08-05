"""Seed the database with the builtin strategies (as CANDIDATE rows) and
default system controls. Approval remains a deliberate admin action.

Usage: .venv/bin/python -m scripts.seed
"""

from __future__ import annotations

import asyncio

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models import (
    CONTROL_CIRCUIT_BREAKER,
    CONTROL_KILL_SWITCH,
    CONTROL_LIVE_ARMED,
    Strategy,
    SystemControl,
)
from app.logging import configure_logging, get_logger
from app.strategies import registry

log = get_logger("scripts.seed")

BUILTINS = [
    {
        "name": "sma_cross",
        "class_path": "app.strategies.builtin.sma_cross.SmaCross",
        "params": {"fast": 10, "slow": 20, "qty": 1},
        "description": "Example SMA crossover (long-only). Not investment advice.",
    },
    {
        "name": "rsi_reversion",
        "class_path": "app.strategies.builtin.rsi_reversion.RsiReversion",
        "params": {"period": 14, "oversold": 30, "overbought": 70, "qty": 1},
        "description": "Example RSI mean-reversion (long-only). Not investment advice.",
    },
    # --- Researched catalog (see docs/strategies.md for lineage & caveats) ---
    {
        "name": "tsmom",
        "class_path": "app.strategies.builtin.tsmom.TimeSeriesMomentum",
        "params": {
            "lookback_days": 252, "skip_days": 0, "eval_frequency": "monthly", "qty": 1,
        },
        "description": (
            "Time-series momentum, Moskowitz/Ooi/Pedersen (2012): long while the "
            "trailing 12-month return is positive, monthly cadence. Long-only port."
        ),
    },
    {
        "name": "turtle_s1",
        "class_path": "app.strategies.builtin.donchian_breakout.DonchianBreakout",
        "params": {
            "entry_days": 20, "exit_days": 10, "atr_period": 20,
            "stop_atr_mult": 2.0, "qty": 1,
        },
        "description": (
            "Turtle System 1 (Dennis/Eckhardt via Curtis Faith): 20-day Donchian "
            "breakout, 10-day exit, 2N ATR stop. No pyramiding/winner-filter."
        ),
    },
    {
        "name": "turtle_s2",
        "class_path": "app.strategies.builtin.donchian_breakout.DonchianBreakout",
        "params": {
            "entry_days": 55, "exit_days": 20, "atr_period": 20,
            "stop_atr_mult": 2.0, "qty": 1,
        },
        "description": (
            "Turtle System 2: 55-day Donchian breakout, 20-day exit, 2N ATR stop. "
            "All breakouts taken (no filter, per the original rules)."
        ),
    },
    {
        "name": "connors_rsi2",
        "class_path": "app.strategies.builtin.connors_rsi2.ConnorsRsi2",
        "params": {
            "rsi_period": 2, "entry_threshold": 10, "trend_sma": 200,
            "exit_mode": "sma", "exit_sma": 5, "qty": 1,
        },
        "description": (
            "Connors RSI-2 pullback (Connors/Alvarez 2008): buy RSI(2)<10 above "
            "the 200-day SMA, exit on close above the 5-day SMA. No stop by design."
        ),
    },
    {
        "name": "double7",
        "class_path": "app.strategies.builtin.double7.ConnorsDouble7",
        "params": {"entry_lookback": 7, "exit_lookback": 7, "trend_sma": 200, "qty": 1},
        "description": (
            "Connors Double 7s (2008): buy 7-day closing low above the 200-day "
            "SMA, exit on a 7-day closing high. No stop by design."
        ),
    },
    {
        "name": "bollinger_reversion",
        "class_path": "app.strategies.builtin.bollinger_reversion.BollingerReversion",
        "params": {"period": 20, "num_std": 2.0, "trend_sma": 200, "qty": 1},
        "description": (
            "Bollinger band reversion (bands per John Bollinger): buy below the "
            "lower band in an uptrend (200-SMA gate), exit at the middle band."
        ),
    },
    {
        "name": "high_52w",
        "class_path": "app.strategies.builtin.high_52w.FiftyTwoWeekHigh",
        "params": {
            "window": 252, "entry_ratio": 0.95, "exit_ratio": 0.80,
            "high_basis": "high", "qty": 1,
        },
        "description": (
            "52-week-high momentum (George/Hwang 2004, single-symbol proximity "
            "adaptation): buy within 5% of the 52-week high, exit at -20%."
        ),
    },
    # --- Persona engine, Phase 1 (see docs/spec-persona-engine.md) ---
    {
        "name": "ptj_trend",
        "class_path": "app.strategies.builtin.ptj_trend.TrendRegime200",
        "params": {"sma_period": 200, "qty": 1},
        "description": (
            "Paul Tudor Jones' 200-day rule: long above the 200-day SMA, flat "
            "below it. Complete fidelity to his documented public heuristic."
        ),
    },
    {
        "name": "minervini_trend_template",
        "class_path": "app.strategies.builtin.minervini.MinerviniTrendTemplate",
        "params": {
            "sma_fast": 50, "sma_mid": 150, "sma_slow": 200,
            "slow_rising_days": 21, "window_52w": 252,
            "low_multiple": 1.25, "high_ratio": 0.75,
            "exit_mode": "template_fail", "qty": 1,
        },
        "description": (
            "Minervini Trend Template (SEPA Stage 2 screen), all 8 criteria. "
            "Criterion 8 (IBD RS>=70) approximated by benchmark-relative return; "
            "flagged rs_skipped when no benchmark data is available."
        ),
    },
    # --- Persona engine, Phase 3 (partial-fidelity, clearly labelled) ---
    {
        "name": "oneil_breakout",
        "class_path": "app.strategies.builtin.oneil.ONeilBreakout",
        "params": {
            "base_lookback": 50, "volume_window": 50, "volume_multiple": 1.5,
            "stop_pct": 0.08, "target_pct": 0.25, "qty": 1,
        },
        "description": (
            "O'Neil CAN SLIM TECHNICAL SUBSET: base breakout on 1.5x volume, "
            "market-direction and leader filters, 8% stop / 25% target. The C, A "
            "and I letters are absent (no earnings or ownership data)."
        ),
    },
    {
        "name": "sweep_reversal",
        "class_path": "app.strategies.builtin.sweep_reversal.SweepReversal",
        "params": {
            "lookback": 20, "min_low_age": 4, "stop_pct": 0.03,
            "target_pct": 0.06, "qty": 1,
        },
        "description": (
            "Daily failed-breakdown reversal (Raschke's Turtle Soup lineage): "
            "price sweeps a 20-day low then closes back above it. NOT TJR's "
            "strategy — that method is session-based intraday."
        ),
    },
    {
        "name": "dual_momentum_spy",
        "class_path": "app.strategies.builtin.dual_momentum.DualMomentum",
        "params": {
            "lookback_days": 252, "peer_symbol": "QQQ", "cash_symbol": "BIL",
            "eval_frequency": "monthly", "qty": 1,
        },
        "description": (
            "Dual Momentum (Antonacci GEM), single-symbol adaptation: hold when "
            "the 12-month return beats both the cash proxy (absolute leg) and "
            "the peer symbol (relative leg). Monthly cadence."
        ),
    },
]

SLEEVE_CLASS = "app.strategies.builtin.target_weight_sleeve.TargetWeightSleeve"

#: Portfolio personas expressed as one target-weight sleeve per leg (see
#: docs/spec-persona-engine.md). Each leg is its own Strategy row, bound to its
#: symbol, and must be approved individually like any other strategy.
PORTFOLIO_SLEEVES = [
    # Harry Browne's Permanent Portfolio: 25% x 4, canonical 15-35% bands.
    ("permanent_portfolio", "Permanent Portfolio (Harry Browne): 25% each, rebalance outside 15-35%.",
     [("VTI", 0.25), ("TLT", 0.25), ("GLD", 0.25), ("BIL", 0.25)], {"band_abs": 0.10}),
    # Ray Dalio retail All Weather approximation (unlevered; NOT Bridgewater's).
    ("all_weather", "All Weather (Dalio retail approximation, unlevered): 30/40/15/7.5/7.5.",
     [("VTI", 0.30), ("TLT", 0.40), ("IEI", 0.15), ("GLD", 0.075), ("DBC", 0.075)],
     {"band_rel": 0.25}),
]


def _sleeve_specs() -> list[dict]:
    specs = []
    for portfolio, description, legs, band in PORTFOLIO_SLEEVES:
        for symbol, weight in legs:
            specs.append(
                {
                    "name": f"{portfolio}_{symbol.lower()}",
                    "class_path": SLEEVE_CLASS,
                    "params": {"symbol": symbol, "target_weight": weight, **band},
                    "description": f"{description} Leg: {symbol} @ {weight:.1%}.",
                }
            )
    return specs


DEFAULT_CONTROLS = {
    CONTROL_KILL_SWITCH: {"engaged": False, "reason": "initial"},
    CONTROL_LIVE_ARMED: {"armed": False},
    CONTROL_CIRCUIT_BREAKER: {"tripped": False, "reason": "initial"},
}


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.debug)
    registry.load_builtins()  # validates that class paths resolve

    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        for spec in [*BUILTINS, *_sleeve_specs()]:
            registry.get_strategy_class(spec["class_path"])  # sanity
            exists = (
                await session.execute(
                    sa.select(Strategy).where(
                        Strategy.name == spec["name"], Strategy.version == 1
                    )
                )
            ).scalar_one_or_none()
            if exists:
                log.info("strategy_exists", name=spec["name"])
                continue
            session.add(Strategy(version=1, **spec))
            log.info("strategy_registered", name=spec["name"], status="candidate")
        for key, value in DEFAULT_CONTROLS.items():
            if await session.get(SystemControl, key) is None:
                session.add(SystemControl(key=key, value=value, updated_by="seed"))
        await session.commit()
    await engine.dispose()
    log.info("seed_complete")


if __name__ == "__main__":
    asyncio.run(main())
