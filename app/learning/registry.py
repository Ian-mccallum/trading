"""Versioned model registry over the ``model_versions`` table.

Lifecycle: REGISTERED (inert) → SHADOW (scores decisions, never trades) →
CHAMPION (drives strategy selection) → RETIRED.

Promotion is deliberately manual: ``promote`` demands a non-empty human
``actor`` and writes a ``PromotionEvent`` audit row for every change. There
is no code path in the platform that calls it automatically — training
registers artifacts and stops.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ModelStatus, ModelVersion, PromotionEvent
from app.logging import get_logger

log = get_logger("learning.registry")

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    ModelStatus.REGISTERED: {ModelStatus.SHADOW, ModelStatus.RETIRED},
    ModelStatus.SHADOW: {ModelStatus.CHAMPION, ModelStatus.RETIRED},
    ModelStatus.CHAMPION: {ModelStatus.RETIRED},
    ModelStatus.RETIRED: set(),
}


class ModelRegistry:
    async def register(
        self,
        session: AsyncSession,
        name: str,
        artifact: dict,
        training_meta: dict,
        metrics: dict,
    ) -> ModelVersion:
        """Store a new immutable model version with status REGISTERED."""
        current_max = (
            await session.execute(
                sa.select(sa.func.max(ModelVersion.version)).where(ModelVersion.name == name)
            )
        ).scalar_one_or_none()
        row = ModelVersion(
            name=name,
            version=(current_max or 0) + 1,
            artifact=artifact,
            training_meta=training_meta,
            metrics=metrics,
            status=ModelStatus.REGISTERED,
        )
        session.add(row)
        await session.flush()
        log.info("model_registered", name=name, version=row.version)
        return row

    async def get(
        self, session: AsyncSession, name: str, version: int | None = None
    ) -> ModelVersion | None:
        stmt = sa.select(ModelVersion).where(ModelVersion.name == name)
        if version is not None:
            stmt = stmt.where(ModelVersion.version == version)
        else:
            stmt = stmt.order_by(ModelVersion.version.desc())
        return (await session.execute(stmt.limit(1))).scalars().first()

    async def latest_by_status(
        self, session: AsyncSession, name: str, status: ModelStatus
    ) -> ModelVersion | None:
        stmt = (
            sa.select(ModelVersion)
            .where(ModelVersion.name == name, ModelVersion.status == status)
            .order_by(ModelVersion.version.desc())
            .limit(1)
        )
        return (await session.execute(stmt)).scalars().first()

    async def promote(
        self,
        session: AsyncSession,
        model_id: uuid.UUID,
        to_status: ModelStatus,
        actor: str,
        reason: str,
    ) -> ModelVersion:
        """Human-gated status change. Raises ValueError on blank actor, unknown
        model, or an illegal transition. Promoting to CHAMPION retires any
        current champion of the same name first (single-champion invariant)."""
        if not actor or not actor.strip():
            raise ValueError("promotion requires a non-empty human actor")
        row = await session.get(ModelVersion, model_id)
        if row is None:
            raise ValueError(f"model {model_id} not found")
        if to_status not in _ALLOWED_TRANSITIONS.get(row.status, set()):
            raise ValueError(f"illegal transition {row.status} -> {to_status}")

        if to_status == ModelStatus.CHAMPION:
            champions = (
                (
                    await session.execute(
                        sa.select(ModelVersion).where(
                            ModelVersion.name == row.name,
                            ModelVersion.status == ModelStatus.CHAMPION,
                            ModelVersion.id != row.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for champ in champions:
                champ.status = ModelStatus.RETIRED
                session.add(
                    PromotionEvent(
                        subject_type="model",
                        subject_id=champ.id,
                        from_status=ModelStatus.CHAMPION,
                        to_status=ModelStatus.RETIRED,
                        actor=actor,
                        reason=f"displaced by {row.name} v{row.version}",
                    )
                )

        from_status = row.status
        row.status = to_status
        session.add(
            PromotionEvent(
                subject_type="model",
                subject_id=row.id,
                from_status=from_status,
                to_status=to_status,
                actor=actor,
                reason=reason,
            )
        )
        await session.flush()
        log.info(
            "model_promoted",
            name=row.name,
            version=row.version,
            from_status=from_status,
            to_status=str(to_status),
            actor=actor,
        )
        return row
