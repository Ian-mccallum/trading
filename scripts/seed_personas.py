"""Seed the persona registry. Idempotent; run after scripts.seed.

Every persona is created INACTIVE — activation is a deliberate, audited admin
action (POST /admin/personas/{id}/activate). Fidelity notes are copied from
docs/investor-personas-research.md so the honest caveats travel with the data,
not just the documentation.

Usage: .venv/bin/python -m scripts.seed_personas
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models import PersonaKind
from app.logging import configure_logging, get_logger
from app.personas.service import upsert_persona

log = get_logger("scripts.seed_personas")

PERSONAS = [
    {
        "name": "paul_tudor_jones",
        "kind": PersonaKind.COMPETING,
        "description": "Long above the 200-day moving average, flat below it.",
        "fidelity_note": (
            "COMPLETE fidelity. 'Nothing good happens below the 200-day moving "
            "average' is his documented public heuristic and maps exactly to a "
            "long-only platform. The optional 5:1 bracket is an interpretation "
            "of his risk discipline, not a published mechanical rule, and is off "
            "by default."
        ),
        "symbol_set": ["SPY", "QQQ", "AAPL", "MSFT"],
        "member_strategy_names": ["ptj_trend"],
    },
    {
        "name": "minervini",
        "kind": PersonaKind.COMPETING,
        "description": "SEPA Trend Template — the Stage 2 uptrend screen.",
        "fidelity_note": (
            "HIGH fidelity: 7 of 8 criteria are exact price/MA relationships. "
            "Criterion 8 (IBD RS >= 70) is a universe percentile that a "
            "single-symbol platform cannot compute; it is approximated by "
            "benchmark-relative return and flagged rs_skipped when no benchmark "
            "is available. The Trend Template is a SCREEN — Minervini's actual "
            "entries are VCP breakouts and his exits are stop-driven, so using "
            "the template as entry/exit is a documented extension. VCP itself is "
            "not implemented: which swings count as contractions is discretionary."
        ),
        "symbol_set": ["AAPL", "MSFT", "QQQ"],
        "member_strategy_names": ["minervini_trend_template"],
    },
    {
        "name": "permanent_portfolio",
        "kind": PersonaKind.COOPERATIVE,
        "description": "Harry Browne: 25% each stocks / long bonds / gold / cash.",
        "fidelity_note": (
            "HIGH fidelity via one target-weight sleeve per leg, including "
            "Browne's canonical rule of rebalancing only when a leg leaves the "
            "15-35%% band rather than on a calendar. The only loss versus a true "
            "portfolio strategy is that legs rebalance independently rather than "
            "atomically, which is acceptable at daily cadence."
        ),
        "symbol_set": ["VTI", "TLT", "GLD", "BIL"],
        "member_strategy_names": [
            "permanent_portfolio_vti", "permanent_portfolio_tlt",
            "permanent_portfolio_gld", "permanent_portfolio_bil",
        ],
    },
    {
        "name": "all_weather",
        "kind": PersonaKind.COOPERATIVE,
        "description": "Ray Dalio All Weather (retail): 30/40/15/7.5/7.5.",
        "fidelity_note": (
            "APPROXIMATION, deliberately labeled. This is the widely published "
            "retail allocation, NOT Bridgewater's actual All Weather, which uses "
            "leverage and futures to equalize risk contributions. The unlevered "
            "version is a fixed-weight portfolio inspired by the concept. Its "
            "backtested record is also flattered by the multi-decade bond bull "
            "market that ended in 2022, when the 40%% long-duration sleeve was "
            "severely punished."
        ),
        "symbol_set": ["VTI", "TLT", "IEI", "GLD", "DBC"],
        "member_strategy_names": [
            "all_weather_vti", "all_weather_tlt", "all_weather_iei",
            "all_weather_gld", "all_weather_dbc",
        ],
    },
    {
        "name": "oneil",
        "kind": PersonaKind.COMPETING,
        "description": "CAN SLIM technical subset — base breakouts on volume.",
        "fidelity_note": (
            "PARTIAL by necessity. CAN SLIM is half fundamental: C (quarterly "
            "EPS +25%), A (annual EPS +25%) and I (institutional sponsorship) "
            "need earnings and ownership data this platform does not have, and "
            "are simply absent. What is implemented is N (breakout from a base), "
            "S (volume >= 1.5x average), M (market above its long-term trend) and "
            "an approximation of L (benchmark-relative return, not IBD's RS "
            "percentile), plus O'Neil's 8%% stop and 25%% target, which are the "
            "system's risk spine. Every decision records which letters ran."
        ),
        "symbol_set": ["AAPL", "MSFT", "QQQ", "SPY"],
        "member_strategy_names": ["oneil_breakout"],
    },
    {
        "name": "sweep_reversal",
        "kind": PersonaKind.COMPETING,
        "description": "Daily failed-breakdown reversal after a liquidity sweep.",
        "fidelity_note": (
            "NOT TJR, and must never be presented as such. TJR's method is "
            "session-based intraday (Asia-session liquidity pools swept during "
            "the London and New York opens, fair value gaps, 1m-15m entries); "
            "daily bars have no sessions, so that structure does not exist here. "
            "This implements the same underlying idea on daily bars, where it is "
            "a long-documented standalone pattern published by Linda Raschke as "
            "Turtle Soup in Street Smarts (1995)."
        ),
        "symbol_set": ["SPY", "QQQ", "AAPL", "MSFT"],
        "member_strategy_names": ["sweep_reversal"],
    },
    {
        "name": "dual_momentum",
        "kind": PersonaKind.COMPETING,
        "description": "Gary Antonacci's Global Equity Momentum, adapted.",
        "fidelity_note": (
            "ADAPTED. GEM is a rotation between US equities, foreign equities and "
            "bonds; a single-symbol platform cannot rotate, so this asks 'should "
            "this symbol be held?' — its 12-month return must beat both a cash "
            "proxy (absolute leg) and a peer symbol (relative leg). Without peer "
            "data the relative leg is skipped and flagged, at which point it is "
            "plain absolute momentum, equivalent to tsmom. The bond leg is not "
            "modelled: pair it with a bond sleeve to reproduce the intent."
        ),
        "symbol_set": ["SPY", "QQQ", "BIL"],
        "member_strategy_names": ["dual_momentum_spy"],
    },
    {
        "name": "quant_classics",
        "kind": PersonaKind.COMPETING,
        "description": (
            "The researched systematic catalog — TSMOM, Turtle, Connors, "
            "Bollinger, 52-week high — competing under allocator selection."
        ),
        "fidelity_note": (
            "Published academic and practitioner systems; see docs/strategies.md "
            "for each one's lineage and flagged simplifications (no Turtle "
            "pyramiding or winner-filter; TSMOM uses no skip month by design)."
        ),
        "symbol_set": ["SPY", "QQQ", "AAPL", "MSFT"],
        "member_strategy_names": [
            "tsmom", "turtle_s1", "turtle_s2", "connors_rsi2",
            "double7", "bollinger_reversion", "high_52w",
        ],
    },
]


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.debug)
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    created = 0
    async with factory() as session:
        for spec in PERSONAS:
            persona = await upsert_persona(session, **spec)
            if persona is None:
                log.info("persona_skipped", name=spec["name"], reason="exists_or_missing_member")
                continue
            created += 1
            log.info(
                "persona_registered",
                name=persona.name,
                kind=persona.kind,
                status=persona.status,
                members=len(spec["member_strategy_names"]),
            )
        await session.commit()
    await engine.dispose()
    log.info("seed_personas_complete", created=created)


if __name__ == "__main__":
    asyncio.run(main())
