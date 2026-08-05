"""Phase 2 tests: persona registry, activation lifecycle, and the cooperative
orchestration fix.

The orchestration tests are the substance: before this phase the allocator
picked one strategy per symbol and shadowed the rest, which would have held a
single leg of All Weather and silently shadowed the other four.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    DecisionMode,
    Persona,
    PersonaKind,
    PersonaMember,
    PersonaStatus,
    PromotionEvent,
    Strategy,
    StrategyStatus,
)
from app.personas.orchestration import PersonaBinding, assign_modes, competing_pool
from app.personas.service import (
    activate,
    deactivate,
    get_persona,
    load_bindings,
    member_strategies,
    upsert_persona,
)

# ---------------------------------------------------------------- helpers


def sid() -> uuid.UUID:
    return uuid.uuid4()


def binding(kind: str, status: str, ids: list[uuid.UUID], name="p") -> PersonaBinding:
    return PersonaBinding(name=name, kind=kind, status=status, strategy_ids=frozenset(ids))


class FakeStrategy:
    """Minimal stand-in for a Strategy row in pure orchestration tests."""

    def __init__(self, id_: uuid.UUID, name: str = "s"):
        self.id = id_
        self.name = name


# ================================================================ orchestration


def test_cooperative_members_all_trade():
    """The Phase 0 bug: 5 All Weather legs, allocator picks 1, 4 get shadowed."""
    legs = [sid() for _ in range(5)]
    bindings = [binding(PersonaKind.COOPERATIVE, PersonaStatus.ACTIVE, legs, "all_weather")]
    modes = assign_modes(legs, bindings, selected_id=legs[0])
    assert all(m == DecisionMode.PAPER for m in modes.values())


def test_competing_members_use_allocator_selection():
    a, b, c = sid(), sid(), sid()
    bindings = [binding(PersonaKind.COMPETING, PersonaStatus.ACTIVE, [a, b, c])]
    modes = assign_modes([a, b, c], bindings, selected_id=b)
    assert modes[b] == DecisionMode.PAPER
    assert modes[a] == modes[c] == DecisionMode.SHADOW


def test_inactive_persona_members_are_shadow_only():
    legs = [sid(), sid()]
    bindings = [binding(PersonaKind.COOPERATIVE, PersonaStatus.INACTIVE, legs)]
    modes = assign_modes(legs, bindings, selected_id=legs[0])
    assert all(m == DecisionMode.SHADOW for m in modes.values())


def test_retired_persona_members_are_shadow_only():
    legs = [sid()]
    bindings = [binding(PersonaKind.COOPERATIVE, PersonaStatus.RETIRED, legs)]
    assert assign_modes(legs, bindings, legs[0])[legs[0]] == DecisionMode.SHADOW


def test_inactive_membership_wins_over_active_cooperative():
    """A strategy in both an active cooperative and an inactive persona must
    not trade — the conservative rule wins."""
    shared = sid()
    bindings = [
        binding(PersonaKind.COOPERATIVE, PersonaStatus.ACTIVE, [shared], "active_one"),
        binding(PersonaKind.COMPETING, PersonaStatus.INACTIVE, [shared], "inactive_one"),
    ]
    assert assign_modes([shared], bindings, None)[shared] == DecisionMode.SHADOW


def test_unaffiliated_strategies_still_compete():
    """Strategies in no persona behave exactly as before Phase 2."""
    a, b = sid(), sid()
    modes = assign_modes([a, b], [], selected_id=a)
    assert modes[a] == DecisionMode.PAPER
    assert modes[b] == DecisionMode.SHADOW


def test_no_selection_means_all_shadow():
    a, b = sid(), sid()
    modes = assign_modes([a, b], [], selected_id=None)
    assert all(m == DecisionMode.SHADOW for m in modes.values())


def test_mixed_cooperative_and_competing():
    """All Weather's legs all trade while separate personas still compete."""
    legs = [sid() for _ in range(3)]
    rivals = [sid(), sid()]
    bindings = [
        binding(PersonaKind.COOPERATIVE, PersonaStatus.ACTIVE, legs, "all_weather"),
        binding(PersonaKind.COMPETING, PersonaStatus.ACTIVE, rivals, "classics"),
    ]
    modes = assign_modes(legs + rivals, bindings, selected_id=rivals[1])
    assert all(modes[leg] == DecisionMode.PAPER for leg in legs)
    assert modes[rivals[1]] == DecisionMode.PAPER
    assert modes[rivals[0]] == DecisionMode.SHADOW


# ---------------------------------------------------------------- competing pool


def test_competing_pool_excludes_cooperative_and_inactive():
    coop = [FakeStrategy(sid()) for _ in range(2)]
    inactive = [FakeStrategy(sid())]
    free = [FakeStrategy(sid()), FakeStrategy(sid())]
    bindings = [
        binding(PersonaKind.COOPERATIVE, PersonaStatus.ACTIVE, [s.id for s in coop]),
        binding(PersonaKind.COMPETING, PersonaStatus.INACTIVE, [s.id for s in inactive]),
    ]
    pool = competing_pool(coop + inactive + free, bindings)
    assert {s.id for s in pool} == {s.id for s in free}


def test_competing_pool_keeps_active_competing_members():
    rivals = [FakeStrategy(sid()), FakeStrategy(sid())]
    bindings = [
        binding(PersonaKind.COMPETING, PersonaStatus.ACTIVE, [s.id for s in rivals])
    ]
    assert len(competing_pool(rivals, bindings)) == 2


def test_competing_pool_empty_bindings_passthrough():
    rows = [FakeStrategy(sid()) for _ in range(3)]
    assert len(competing_pool(rows, [])) == 3


# ================================================================ persistence


async def seed_strategy(session, name: str, status=StrategyStatus.CANDIDATE) -> Strategy:
    row = Strategy(
        name=name, version=1, class_path=f"x.{name}", params={}, status=status
    )
    session.add(row)
    await session.flush()
    return row


async def test_upsert_creates_persona_with_members(db_session):
    await seed_strategy(db_session, "leg_a")
    await seed_strategy(db_session, "leg_b")
    persona = await upsert_persona(
        db_session, name="test_portfolio", description="d", fidelity_note="f",
        kind=PersonaKind.COOPERATIVE, symbol_set=["VTI", "TLT"],
        member_strategy_names=["leg_a", "leg_b"],
    )
    assert persona is not None
    assert persona.status == PersonaStatus.INACTIVE  # never auto-active
    assert len(await member_strategies(db_session, persona)) == 2


async def test_upsert_is_idempotent(db_session):
    await seed_strategy(db_session, "leg_a")
    kwargs = dict(
        name="dup", description="d", fidelity_note="f", kind=PersonaKind.COMPETING,
        symbol_set=[], member_strategy_names=["leg_a"],
    )
    assert await upsert_persona(db_session, **kwargs) is not None
    assert await upsert_persona(db_session, **kwargs) is None  # second is a no-op


async def test_upsert_refuses_partial_persona(db_session):
    """A missing member must not produce half a persona."""
    await seed_strategy(db_session, "leg_a")
    persona = await upsert_persona(
        db_session, name="broken", description="d", fidelity_note="f",
        kind=PersonaKind.COOPERATIVE, symbol_set=[],
        member_strategy_names=["leg_a", "does_not_exist"],
    )
    assert persona is None
    count = (
        await db_session.execute(sa.select(sa.func.count(Persona.id)))
    ).scalar_one()
    assert count == 0


# ---------------------------------------------------------------- activation


async def make_persona(db_session, names: list[str], kind=PersonaKind.COOPERATIVE):
    for n in names:
        await seed_strategy(db_session, n)
    return await upsert_persona(
        db_session, name="p1", description="d", fidelity_note="f", kind=kind,
        symbol_set=[], member_strategy_names=names,
    )


async def test_activate_approves_candidate_members(db_session):
    persona = await make_persona(db_session, ["leg_a", "leg_b"])
    result = await activate(db_session, persona, actor="ian", reason="reviewed backtests")

    assert persona.status == PersonaStatus.ACTIVE
    assert sorted(result["approved"]) == ["leg_a", "leg_b"]
    for strategy in await member_strategies(db_session, persona):
        assert strategy.status == StrategyStatus.APPROVED
        assert strategy.approved_by == "ian"


async def test_activate_writes_audit_rows(db_session):
    """Activation must be as auditable as manual approval — one event per
    member plus one for the persona itself."""
    persona = await make_persona(db_session, ["leg_a", "leg_b"])
    await activate(db_session, persona, actor="ian", reason="reviewed")

    events = (await db_session.execute(sa.select(PromotionEvent))).scalars().all()
    subjects = [e.subject_type for e in events]
    assert subjects.count("strategy") == 2
    assert subjects.count("persona") == 1
    assert all(e.actor == "ian" for e in events)
    assert any("persona activation" in (e.reason or "") for e in events)


async def test_activate_requires_actor(db_session):
    persona = await make_persona(db_session, ["leg_a"])
    with pytest.raises(ValueError, match="actor"):
        await activate(db_session, persona, actor="   ", reason="r")


async def test_activate_refuses_empty_persona(db_session):
    persona = Persona(name="empty", kind=PersonaKind.COMPETING, symbol_set=[])
    db_session.add(persona)
    await db_session.flush()
    with pytest.raises(ValueError, match="no members"):
        await activate(db_session, persona, actor="ian", reason="r")


async def test_activate_reports_already_tradeable_and_retired(db_session):
    await seed_strategy(db_session, "fresh")
    await seed_strategy(db_session, "live_one", status=StrategyStatus.APPROVED)
    await seed_strategy(db_session, "dead", status=StrategyStatus.RETIRED)
    persona = await upsert_persona(
        db_session, name="mixed", description="d", fidelity_note="f",
        kind=PersonaKind.COMPETING, symbol_set=[],
        member_strategy_names=["fresh", "live_one", "dead"],
    )
    result = await activate(db_session, persona, actor="ian", reason="r")

    assert result["approved"] == ["fresh"]
    assert result["already_tradeable"] == ["live_one"]
    # Retirement is terminal: reported, never silently resurrected.
    assert result["blocked_retired"] == ["dead"]
    dead = (
        await db_session.execute(sa.select(Strategy).where(Strategy.name == "dead"))
    ).scalars().one()
    assert dead.status == StrategyStatus.RETIRED


async def test_deactivate_is_reversible(db_session):
    """Deactivation stops trading without any terminal status change."""
    persona = await make_persona(db_session, ["leg_a"])
    await activate(db_session, persona, actor="ian", reason="on")
    await deactivate(db_session, persona, actor="ian", reason="off")

    assert persona.status == PersonaStatus.INACTIVE
    strategy = (await member_strategies(db_session, persona))[0]
    assert strategy.status == StrategyStatus.APPROVED  # still approved, just idle

    # And it can be turned back on.
    await activate(db_session, persona, actor="ian", reason="back on")
    assert persona.status == PersonaStatus.ACTIVE


async def test_deactivated_persona_members_go_shadow(db_session):
    """The end-to-end guarantee: deactivating pulls members out of PAPER."""
    persona = await make_persona(db_session, ["leg_a", "leg_b"])
    await activate(db_session, persona, actor="ian", reason="on")
    ids = [s.id for s in await member_strategies(db_session, persona)]

    bindings = await load_bindings(db_session)
    assert all(m == DecisionMode.PAPER for m in assign_modes(ids, bindings, None).values())

    await deactivate(db_session, persona, actor="ian", reason="off")
    bindings = await load_bindings(db_session)
    assert all(m == DecisionMode.SHADOW for m in assign_modes(ids, bindings, None).values())


async def test_load_bindings_reflects_membership(db_session):
    persona = await make_persona(db_session, ["leg_a", "leg_b"])
    bindings = await load_bindings(db_session)
    assert len(bindings) == 1
    assert bindings[0].name == persona.name
    assert bindings[0].kind == PersonaKind.COOPERATIVE
    assert len(bindings[0].strategy_ids) == 2
    assert not bindings[0].is_active


async def test_get_persona_missing_returns_none(db_session):
    assert await get_persona(db_session, uuid.uuid4()) is None


async def test_persona_member_uniqueness(db_session):
    persona = await make_persona(db_session, ["leg_a"])
    strategy = (await member_strategies(db_session, persona))[0]
    db_session.add(PersonaMember(persona_id=persona.id, strategy_id=strategy.id))
    with pytest.raises(IntegrityError):  # uq_persona_member
        await db_session.flush()
