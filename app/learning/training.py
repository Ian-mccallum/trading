"""Offline allocator training.

Reads CLOSED decisions (paper/live/shadow) inside the lookback window and
builds a ``regime_scores_v1`` artifact: per (regime, strategy) exponentially
time-weighted mean return, plus per-strategy defaults across regimes. The
result is registered as a new *inert* model version (status REGISTERED); a
human promotes it to SHADOW/CHAMPION via the admin API after review.

This module never touches orders, strategies' statuses, or the risk engine.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Decision, DecisionMode, DecisionStatus, ModelVersion, Strategy
from app.learning.allocator import ARTIFACT_TYPE, DEFAULT_MODEL_NAME
from app.learning.registry import ModelRegistry
from app.logging import get_logger

log = get_logger("learning.training")

TRAINABLE_MODES = (DecisionMode.PAPER, DecisionMode.LIVE, DecisionMode.SHADOW)


async def train_allocator(
    session: AsyncSession,
    now: datetime,
    lookback_days: int = 30,
    half_life_days: float = 7.0,
    name: str = DEFAULT_MODEL_NAME,
) -> ModelVersion:
    """Train and register a new allocator version. Raises ValueError when no
    qualifying decisions exist (an empty model must not be registered)."""
    cutoff = now - timedelta(days=lookback_days)
    decisions = (
        (
            await session.execute(
                sa.select(Decision).where(
                    Decision.status == DecisionStatus.CLOSED,
                    Decision.strategy_id.is_not(None),
                    Decision.mode.in_(TRAINABLE_MODES),
                    Decision.created_at >= cutoff,
                )
            )
        )
        .scalars()
        .all()
    )

    strategy_names: dict = {}
    for row in (await session.execute(sa.select(Strategy))).scalars().all():
        strategy_names[row.id] = row.name

    # (regime, strategy_name) -> [weight, weighted_return_sum]
    acc: dict[tuple[str, str], list[float]] = {}
    n_used = 0
    for d in decisions:
        ret = (d.outcome or {}).get("return_pct")
        strategy_name = strategy_names.get(d.strategy_id)
        if ret is None or strategy_name is None:
            continue
        created = d.created_at if d.created_at.tzinfo else d.created_at.replace(tzinfo=UTC)
        age_days = max((now - created).total_seconds() / 86_400.0, 0.0)
        weight = 0.5 ** (age_days / half_life_days)
        regime = (d.context or {}).get("regime", "unknown")
        slot = acc.setdefault((regime, strategy_name), [0.0, 0.0])
        slot[0] += weight
        slot[1] += weight * float(ret)
        n_used += 1

    if n_used == 0:
        raise ValueError("no closed decisions with outcomes in the lookback window")

    scores: dict[str, dict[str, float]] = {}
    by_strategy: dict[str, list[float]] = {}
    for (regime, strategy_name), (weight_sum, weighted_ret) in acc.items():
        mean = weighted_ret / weight_sum if weight_sum else 0.0
        scores.setdefault(regime, {})[strategy_name] = round(mean, 8)
        by_strategy.setdefault(strategy_name, [0.0, 0.0])
        by_strategy[strategy_name][0] += weight_sum
        by_strategy[strategy_name][1] += weighted_ret
    default = {
        s_name: round(wret / wsum, 8) if wsum else 0.0
        for s_name, (wsum, wret) in by_strategy.items()
    }

    artifact = {"type": ARTIFACT_TYPE, "scores": scores, "default": default}
    model = await ModelRegistry().register(
        session,
        name=name,
        artifact=artifact,
        training_meta={
            "lookback_days": lookback_days,
            "half_life_days": half_life_days,
            "n_decisions": n_used,
            "trained_at": now.isoformat(),
        },
        metrics={"n_decisions": n_used, "regimes": sorted(scores)},
    )
    log.info("allocator_trained", version=model.version, n_decisions=n_used)
    return model
