"""Trading-mode assignment across personas.

This module fixes the orchestration gap Phase 0 exposed. The strategy loop
previously asked the allocator to pick **one** strategy per symbol for PAPER
and shadowed the rest. That is correct for *competing* strategies and wrong
for *cooperating* ones: All Weather's five sleeves are a single portfolio, so
picking one would hold 40% TLT and silently shadow the other four legs.

Rules, in priority order:

1. A strategy belonging to an **inactive/retired** persona is SHADOW-only.
   This is what makes deactivation safe and reversible — no destructive
   status change is needed to stop a persona trading, and members can never
   silently fall back into the competing pool.
2. A strategy belonging to an **active cooperative** persona always trades
   (PAPER). Its legs are a portfolio, not candidates.
3. Everything else forms the competing pool, where the champion allocator
   selects one per regime exactly as before. Non-selected members run SHADOW
   so the learner still collects counterfactuals.

Pure and synchronous by design: no DB, no broker, no clock — so the routing
policy is unit-testable in isolation from everything it routes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.db.models import DecisionMode, PersonaKind, PersonaStatus


@dataclass(frozen=True)
class PersonaBinding:
    """Flattened view of one persona and the strategies it owns."""

    name: str
    kind: str
    status: str
    strategy_ids: frozenset[uuid.UUID]

    @property
    def is_active(self) -> bool:
        return self.status == PersonaStatus.ACTIVE

    @property
    def is_cooperative(self) -> bool:
        return self.kind == PersonaKind.COOPERATIVE


def assign_modes(
    strategy_ids: list[uuid.UUID],
    bindings: list[PersonaBinding],
    selected_id: uuid.UUID | None,
) -> dict[uuid.UUID, str]:
    """Map each approved strategy to the mode it should trade in.

    ``selected_id`` is the allocator's pick from the *competing pool* — see
    ``competing_pool`` for building that pool before calling the allocator.
    """
    shadow_only: set[uuid.UUID] = set()
    cooperative: set[uuid.UUID] = set()
    for binding in bindings:
        if binding.is_active and binding.is_cooperative:
            cooperative |= binding.strategy_ids
        elif not binding.is_active:
            shadow_only |= binding.strategy_ids

    modes: dict[uuid.UUID, str] = {}
    for sid in strategy_ids:
        # Rule 1 wins: an inactive persona's members never trade, even if the
        # same strategy is also a member of an active cooperative persona.
        if sid in shadow_only:
            modes[sid] = DecisionMode.SHADOW
        elif sid in cooperative or (selected_id is not None and sid == selected_id):
            modes[sid] = DecisionMode.PAPER
        else:
            modes[sid] = DecisionMode.SHADOW
    return modes


def competing_pool(strategies: list, bindings: list[PersonaBinding]) -> list:
    """Strategies eligible for allocator selection.

    Excludes members of active cooperative personas (they always trade) and
    members of inactive personas (they never do), leaving genuine
    alternatives for the allocator to choose between.
    """
    excluded: set[uuid.UUID] = set()
    for binding in bindings:
        if not binding.is_active or binding.is_cooperative:
            excluded |= binding.strategy_ids
    return [s for s in strategies if s.id not in excluded]
