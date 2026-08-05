"""Persona persistence helpers: loading bindings and activating personas.

Activation deliberately reuses the *existing* audited strategy-approval path
— it approves each member through the same transition rules and writes the
same ``PromotionEvent`` rows an operator would produce by hand. It is
convenience sugar over the existing endpoint, never a bypass.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.base import utcnow
from app.db.models import (
    Persona,
    PersonaMember,
    PersonaStatus,
    PromotionEvent,
    Strategy,
    StrategyStatus,
)
from app.logging import get_logger
from app.personas.orchestration import PersonaBinding

log = get_logger("personas.service")

#: Statuses a member must reach for its persona to trade it.
TRADEABLE_STATUSES = (StrategyStatus.APPROVED, StrategyStatus.CHAMPION)


async def load_personas(session: AsyncSession) -> list[Persona]:
    rows = (
        await session.execute(
            sa.select(Persona).options(selectinload(Persona.members)).order_by(Persona.name)
        )
    ).scalars().all()
    return list(rows)


async def load_bindings(session: AsyncSession) -> list[PersonaBinding]:
    """Flattened bindings for the strategy loop's mode assignment.

    One query for personas and one for memberships — the loop runs every
    cycle, so this stays O(1) queries rather than O(personas).
    """
    personas = await load_personas(session)
    rows = (await session.execute(sa.select(PersonaMember))).scalars().all()
    by_persona: dict = {}
    for m in rows:
        by_persona.setdefault(m.persona_id, set()).add(m.strategy_id)
    return [
        PersonaBinding(
            name=p.name,
            kind=p.kind,
            status=p.status,
            strategy_ids=frozenset(by_persona.get(p.id, set())),
        )
        for p in personas
    ]


async def get_persona(session: AsyncSession, persona_id: uuid.UUID) -> Persona | None:
    return (
        await session.execute(
            sa.select(Persona)
            .options(selectinload(Persona.members))
            .where(Persona.id == persona_id)
        )
    ).scalars().first()


async def member_strategies(session: AsyncSession, persona: Persona) -> list[Strategy]:
    """Member strategies via an explicit join.

    Deliberately does NOT read ``persona.members``: async SQLAlchemy cannot
    lazy-load a relationship on demand, so depending on it would work only for
    instances that happened to be eagerly loaded and raise MissingGreenlet
    everywhere else.
    """
    rows = (
        await session.execute(
            sa.select(Strategy)
            .join(PersonaMember, PersonaMember.strategy_id == Strategy.id)
            .where(PersonaMember.persona_id == persona.id)
            .order_by(Strategy.name)
        )
    ).scalars().all()
    return list(rows)


async def activate(
    session: AsyncSession, persona: Persona, actor: str, reason: str
) -> dict:
    """Activate a persona, approving any member that is still a candidate.

    Each approval goes through the same legal-transition check and writes the
    same PromotionEvent an operator's manual approval would. Members already
    retired are reported, not silently resurrected — retirement is terminal.
    """
    if not actor.strip():
        raise ValueError("activation requires a non-empty human actor")
    if persona.status == PersonaStatus.RETIRED:
        raise ValueError(f"persona {persona.name!r} is retired and cannot be activated")
    members = await member_strategies(session, persona)
    if not members:
        raise ValueError(f"persona {persona.name!r} has no members")

    approved, already, blocked = [], [], []
    for strategy in members:
        if strategy.status in TRADEABLE_STATUSES:
            already.append(strategy.name)
            continue
        if strategy.status == StrategyStatus.CANDIDATE:
            strategy.status = StrategyStatus.APPROVED
            strategy.approved_by = actor
            strategy.approved_at = utcnow()
            session.add(
                PromotionEvent(
                    subject_type="strategy",
                    subject_id=strategy.id,
                    from_status=StrategyStatus.CANDIDATE,
                    to_status=StrategyStatus.APPROVED,
                    actor=actor,
                    reason=f"persona activation: {persona.name} — {reason}",
                )
            )
            approved.append(strategy.name)
        else:  # retired: terminal, needs a new version rather than revival
            blocked.append(strategy.name)

    persona.status = PersonaStatus.ACTIVE
    persona.activated_by = actor
    persona.activated_at = utcnow()
    session.add(
        PromotionEvent(
            subject_type="persona",
            subject_id=persona.id,
            from_status=PersonaStatus.INACTIVE,
            to_status=PersonaStatus.ACTIVE,
            actor=actor,
            reason=reason,
        )
    )
    await session.flush()
    log.info(
        "persona_activated",
        persona=persona.name,
        approved=approved,
        already_tradeable=already,
        blocked=blocked,
        actor=actor,
    )
    return {"approved": approved, "already_tradeable": already, "blocked_retired": blocked}


async def deactivate(
    session: AsyncSession, persona: Persona, actor: str, reason: str
) -> None:
    """Stop a persona trading. Members stay APPROVED but become shadow-only
    (see orchestration rule 1), so deactivation is safe and reversible — no
    terminal status change is required to pull a persona out of production."""
    if not actor.strip():
        raise ValueError("deactivation requires a non-empty human actor")
    from_status = persona.status
    persona.status = PersonaStatus.INACTIVE
    session.add(
        PromotionEvent(
            subject_type="persona",
            subject_id=persona.id,
            from_status=from_status,
            to_status=PersonaStatus.INACTIVE,
            actor=actor,
            reason=reason,
        )
    )
    await session.flush()
    log.info("persona_deactivated", persona=persona.name, actor=actor, reason=reason)


async def upsert_persona(
    session: AsyncSession,
    *,
    name: str,
    description: str,
    fidelity_note: str,
    kind: str,
    symbol_set: list[str],
    member_strategy_names: list[str],
) -> Persona | None:
    """Idempotent seed helper. Returns None if the persona already exists or
    any member strategy is missing (seeding must not create half a persona)."""
    existing = (
        await session.execute(sa.select(Persona).where(Persona.name == name))
    ).scalars().first()
    if existing is not None:
        return None

    strategies = {}
    for member_name in member_strategy_names:
        row = (
            await session.execute(
                sa.select(Strategy)
                .where(Strategy.name == member_name)
                .order_by(Strategy.version.desc())
                .limit(1)
            )
        ).scalars().first()
        if row is None:
            log.warning("persona_member_missing", persona=name, strategy=member_name)
            return None
        strategies[member_name] = row

    persona = Persona(
        name=name,
        description=description,
        fidelity_note=fidelity_note,
        kind=kind,
        status=PersonaStatus.INACTIVE,
        symbol_set=symbol_set,
    )
    session.add(persona)
    await session.flush()
    for row in strategies.values():
        session.add(PersonaMember(persona_id=persona.id, strategy_id=row.id))
    await session.flush()
    return persona
