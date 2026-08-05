"""Strategy allocator — the *advisory* consumer of trained models.

Artifact format ``regime_scores_v1``::

    {
      "type": "regime_scores_v1",
      "scores":  {"trend_up": {"sma_cross": 0.8, "rsi_reversion": 0.1}, ...},
      "default": {"sma_cross": 0.4, "rsi_reversion": 0.3}
    }

The allocator only ever *ranks the strategy rows handed to it* (and
defensively drops anything not APPROVED/CHAMPION). It returns a
recommendation; it cannot create intents, touch orders, or change statuses.
Whatever it selects still passes through the full risk engine downstream.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ModelStatus, ModelVersion, Strategy, StrategyStatus
from app.logging import get_logger

log = get_logger("learning.allocator")

ARTIFACT_TYPE = "regime_scores_v1"
DEFAULT_MODEL_NAME = "strategy_allocator"

_SELECTABLE = {StrategyStatus.APPROVED.value, StrategyStatus.CHAMPION.value}


class StrategyAllocator:
    def __init__(self, model_version: ModelVersion | None = None) -> None:
        self.model_version = model_version
        artifact = (model_version.artifact or {}) if model_version else {}
        if artifact and artifact.get("type") != ARTIFACT_TYPE:
            log.warning("unknown_artifact_type", type=artifact.get("type"))
            artifact = {}
        self._scores: dict = artifact.get("scores", {})
        self._default: dict = artifact.get("default", {})

    def score(self, regime: str, approved: list[Strategy]) -> list[tuple[Strategy, float]]:
        """Rank the given strategies for the regime, best first. Ties and the
        no-model case fall back to stable name order with 0.0 scores."""
        selectable = [s for s in approved if s.status in _SELECTABLE]
        regime_scores: dict = self._scores.get(regime, {})
        ranked = [
            (s, float(regime_scores.get(s.name, self._default.get(s.name, 0.0))))
            for s in selectable
        ]
        ranked.sort(key=lambda pair: (-pair[1], pair[0].name))
        return ranked

    def select(self, regime: str, approved: list[Strategy]) -> Strategy | None:
        ranked = self.score(regime, approved)
        return ranked[0][0] if ranked else None


async def load_champion_allocator(
    session: AsyncSession, name: str = DEFAULT_MODEL_NAME
) -> StrategyAllocator:
    """Allocator backed by the current CHAMPION model, or a uniform fallback
    allocator when no champion has been promoted yet."""
    from app.learning.registry import ModelRegistry

    champion = await ModelRegistry().latest_by_status(session, name, ModelStatus.CHAMPION)
    return StrategyAllocator(champion)
