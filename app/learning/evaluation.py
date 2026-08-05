"""Decision outcome evaluation and champion/challenger comparison.

``evaluate_decision_outcomes`` closes the loop that makes retraining
possible: every EXECUTED decision (real fills) and every APPROVED shadow
decision (hypothetical entry at its reference price) gets a realized /
hypothetical outcome computed against the latest stored bar, written into
``Decision.outcome``, and moved to CLOSED. Values are stored as strings so
the JSON stays Decimal-exact.

``compare_champion_challenger`` writes an ``Experiment`` row with a
*recommendation only* — it never changes model or strategy statuses.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Decision,
    DecisionMode,
    DecisionStatus,
    Experiment,
    ExperimentKind,
    ExperimentStatus,
    Fill,
    MarketBar,
    ModelVersion,
    Order,
    PerformanceReport,
    SignalAction,
)
from app.logging import get_logger

log = get_logger("learning.evaluation")

MIN_DECISIONS_FOR_RECOMMENDATION = 10


async def _latest_close(session: AsyncSession, symbol: str) -> Decimal | None:
    row = (
        await session.execute(
            sa.select(MarketBar)
            .where(MarketBar.symbol == symbol)
            .order_by(MarketBar.ts.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row.close if row else None


def _signed_pnl(action: str, entry: Decimal, exit_price: Decimal, qty: Decimal) -> Decimal:
    """Buy profits when price rises; sell/close (long-only inventory) profits
    are realized at exit relative to entry, so the sign flips."""
    direction = Decimal(1) if action == SignalAction.BUY else Decimal(-1)
    return (exit_price - entry) * qty * direction


async def evaluate_decision_outcomes(
    session: AsyncSession, now: datetime, horizon_minutes: int = 60
) -> int:
    """Close out decisions older than the horizon. Returns count closed."""
    cutoff = now - timedelta(minutes=horizon_minutes)
    closed = 0

    executed = (
        (
            await session.execute(
                sa.select(Decision).where(
                    Decision.status == DecisionStatus.EXECUTED,
                    Decision.created_at <= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    for decision in executed:
        fills = (
            (
                await session.execute(
                    sa.select(Fill)
                    .join(Order, Fill.order_id == Order.id)
                    .where(Order.decision_id == decision.id)
                )
            )
            .scalars()
            .all()
        )
        if not fills:
            continue  # nothing filled yet; try again next run
        total_qty = sum((f.qty for f in fills), Decimal(0))
        if total_qty <= 0:
            continue
        entry = sum((f.qty * f.price for f in fills), Decimal(0)) / total_qty
        exit_price = await _latest_close(session, decision.symbol)
        if exit_price is None:
            continue
        pnl = _signed_pnl(decision.action, entry, exit_price, total_qty)
        decision.outcome = {
            **(decision.outcome or {}),
            "entry_price": str(entry),
            "exit_price": str(exit_price),
            "realized_pnl": str(pnl),
            "return_pct": str((exit_price / entry - 1) * (1 if decision.action == "buy" else -1))
            if entry
            else "0",
            "horizon_minutes": horizon_minutes,
            "evaluated_at": now.isoformat(),
        }
        decision.status = DecisionStatus.CLOSED
        decision.outcome_recorded_at = now
        closed += 1

    shadows = (
        (
            await session.execute(
                sa.select(Decision).where(
                    Decision.mode == DecisionMode.SHADOW,
                    Decision.status == DecisionStatus.APPROVED,
                    Decision.created_at <= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    for decision in shadows:
        ref = (decision.outcome or {}).get("reference_price")
        if not ref:
            continue
        entry = Decimal(str(ref))
        exit_price = await _latest_close(session, decision.symbol)
        if exit_price is None or entry <= 0:
            continue
        pnl = _signed_pnl(decision.action, entry, exit_price, decision.qty)
        decision.outcome = {
            **decision.outcome,
            "shadow": True,
            "entry_price": str(entry),
            "exit_price": str(exit_price),
            "realized_pnl": str(pnl),
            "return_pct": str((exit_price / entry - 1) * (1 if decision.action == "buy" else -1)),
            "horizon_minutes": horizon_minutes,
            "evaluated_at": now.isoformat(),
        }
        decision.status = DecisionStatus.CLOSED
        decision.outcome_recorded_at = now
        closed += 1

    await session.flush()
    if closed:
        log.info("decisions_closed", count=closed)
    return closed


async def build_performance_reports(
    session: AsyncSession, period_start: datetime, period_end: datetime
) -> list[PerformanceReport]:
    """Aggregate CLOSED decisions per (strategy, regime) into report rows."""
    rows = (
        (
            await session.execute(
                sa.select(Decision).where(
                    Decision.status == DecisionStatus.CLOSED,
                    Decision.strategy_id.is_not(None),
                    Decision.created_at >= period_start,
                    Decision.created_at <= period_end,
                )
            )
        )
        .scalars()
        .all()
    )
    groups: dict[tuple, list[Decision]] = {}
    for d in rows:
        regime = (d.context or {}).get("regime", "unknown")
        groups.setdefault((d.strategy_id, regime, d.mode), []).append(d)

    reports: list[PerformanceReport] = []
    for (strategy_id, regime, mode), decisions in groups.items():
        returns = [
            float(d.outcome["return_pct"]) for d in decisions if d.outcome.get("return_pct")
        ]
        pnls = [
            float(d.outcome["realized_pnl"]) for d in decisions if d.outcome.get("realized_pnl")
        ]
        if not returns:
            continue
        report = PerformanceReport(
            strategy_id=strategy_id,
            model_version_id=decisions[0].model_version_id,
            environment=decisions[0].environment,
            mode=mode,
            regime=regime,
            period_start=period_start,
            period_end=period_end,
            metrics={
                "n": len(returns),
                "total_pnl": sum(pnls),
                "mean_return": sum(returns) / len(returns),
                "win_rate": sum(1 for r in returns if r > 0) / len(returns),
            },
        )
        session.add(report)
        reports.append(report)
    await session.flush()
    return reports


async def compare_champion_challenger(
    session: AsyncSession,
    name: str,
    champion: ModelVersion,
    challenger: ModelVersion,
    period_start: datetime,
    period_end: datetime,
) -> Experiment:
    """Compare CLOSED-decision performance attributed to each model and write
    an Experiment with a recommendation. NEVER changes any status."""

    async def stats(model_id) -> dict:
        decisions = (
            (
                await session.execute(
                    sa.select(Decision).where(
                        Decision.model_version_id == model_id,
                        Decision.status == DecisionStatus.CLOSED,
                        Decision.created_at >= period_start,
                        Decision.created_at <= period_end,
                    )
                )
            )
            .scalars()
            .all()
        )
        returns = [
            float(d.outcome["return_pct"]) for d in decisions if d.outcome.get("return_pct")
        ]
        return {
            "n": len(returns),
            "mean_return": sum(returns) / len(returns) if returns else None,
            "win_rate": (sum(1 for r in returns if r > 0) / len(returns)) if returns else None,
        }

    champ_stats = await stats(champion.id)
    chall_stats = await stats(challenger.id)

    if (
        champ_stats["n"] < MIN_DECISIONS_FOR_RECOMMENDATION
        or chall_stats["n"] < MIN_DECISIONS_FOR_RECOMMENDATION
    ):
        recommendation = "insufficient_data"
    elif chall_stats["mean_return"] > champ_stats["mean_return"]:
        recommendation = "promote_challenger"
    else:
        recommendation = "keep_champion"

    experiment = Experiment(
        name=name,
        kind=ExperimentKind.CHAMPION_CHALLENGER,
        status=ExperimentStatus.COMPLETED,
        champion_model_id=champion.id,
        challenger_model_id=challenger.id,
        config={
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "min_decisions": MIN_DECISIONS_FOR_RECOMMENDATION,
        },
        results={
            "champion": champ_stats,
            "challenger": chall_stats,
            "recommendation": recommendation,
        },
        completed_at=period_end,
    )
    session.add(experiment)
    await session.flush()
    log.info(
        "champion_challenger_compared",
        champion=f"{champion.name} v{champion.version}",
        challenger=f"{challenger.name} v{challenger.version}",
        recommendation=recommendation,
    )
    return experiment
