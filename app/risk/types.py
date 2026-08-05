"""Risk engine contract types.

The risk engine is deliberately independent: it depends only on these types,
the DB, and configuration — never on strategies or the learning system. Every
order path (webhook-driven, strategy-driven, model-driven) passes through
``RiskEngine.evaluate`` before any broker call; there is no bypass parameter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.db.models import Environment, RiskVerdict
from app.schemas.core import AccountState, PositionState, TradeIntent


@dataclass(frozen=True)
class RecentOrder:
    """Minimal view of a recently submitted order, for duplicate detection."""

    symbol: str
    side: str
    strategy_id: Any  # uuid.UUID | None
    created_at: datetime
    status: str


@dataclass(frozen=True)
class RiskContext:
    """Snapshot of the world at evaluation time. Built by the risk engine and
    execution service from broker + DB state; rules only read it."""

    environment: Environment
    now: datetime
    account: AccountState
    positions: list[PositionState]
    last_price: Decimal | None  # latest known price for the intent's symbol
    last_price_ts: datetime | None  # when that price was observed
    day_start_equity: Decimal | None  # equity at session start (daily-loss rule)
    peak_equity: Decimal | None  # historical peak (drawdown rule)
    recent_orders: list[RecentOrder] = field(default_factory=list)
    recent_error_count: int = 0  # broker errors in breaker window
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleResult:
    rule: str
    verdict: RiskVerdict
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.verdict == RiskVerdict.APPROVED


@dataclass(frozen=True)
class RiskDecision:
    """Aggregate outcome. ``approved`` only if every rule approved."""

    verdict: RiskVerdict
    results: list[RuleResult]

    @property
    def approved(self) -> bool:
        return self.verdict == RiskVerdict.APPROVED

    @property
    def rejection_reasons(self) -> list[str]:
        return [r.reason for r in self.results if not r.approved and r.reason]


class RiskRule:
    """A single check. Implementations must be side-effect free; the engine
    handles persistence of results as RiskEvents."""

    name: str = "base"

    def check(self, intent: TradeIntent, ctx: RiskContext) -> RuleResult:  # pragma: no cover
        raise NotImplementedError
