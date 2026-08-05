"""ORM models — the platform's single source of truth for persisted state.

Design notes:

- Every enum is stored as a plain string (``sa.String`` + Python ``StrEnum``)
  to keep Alembic migrations trivial and SQLite-compatible for tests.
- ``Decision`` is the audit spine: every proposed trade — whether it came from
  a TradingView signal, an internal strategy, or a shadow model — becomes a
  Decision row carrying its full feature/market context, the risk verdict, and
  (eventually) its outcome. Learning retrains exclusively from this table plus
  fills; it never mutates production code or state.
- ``environment`` columns keep paper and live rows separated in every
  execution-related table; there is no code path that reads across them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JsonDict, TimestampedBase, utcnow

NUMERIC = sa.Numeric(20, 8)


# ---------------------------------------------------------------- enums


class Environment(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class SignalSource(StrEnum):
    TRADINGVIEW = "tradingview"
    STRATEGY = "strategy"
    MANUAL = "manual"


class SignalStatus(StrEnum):
    RECEIVED = "received"
    VALIDATED = "validated"
    PROCESSED = "processed"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    EXPIRED = "expired"


class SignalAction(StrEnum):
    BUY = "buy"
    SELL = "sell"
    CLOSE = "close"


class StrategyStatus(StrEnum):
    CANDIDATE = "candidate"  # registered, not tradeable
    APPROVED = "approved"  # human-approved: allocator may select it
    CHAMPION = "champion"  # current default strategy
    RETIRED = "retired"


class ModelStatus(StrEnum):
    REGISTERED = "registered"  # trained artifact stored, inert
    SHADOW = "shadow"  # scores decisions, never trades
    CHAMPION = "champion"  # drives strategy selection
    RETIRED = "retired"


class DecisionMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"
    SHADOW = "shadow"  # hypothetical decision, never routed to a broker
    BACKTEST = "backtest"


class DecisionStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"  # risk engine approved
    REJECTED = "rejected"  # risk engine rejected
    EXECUTED = "executed"  # order submitted
    FAILED = "failed"  # broker submission failed
    CLOSED = "closed"  # outcome recorded


class OrderStatus(StrEnum):
    PENDING_SUBMIT = "pending_submit"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    ERROR = "error"

    @classmethod
    def terminal(cls) -> frozenset[OrderStatus]:
        return frozenset({cls.FILLED, cls.CANCELED, cls.REJECTED, cls.EXPIRED, cls.ERROR})


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class RiskVerdict(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    HALTED = "halted"  # kill switch / circuit breaker active


class PersonaKind(StrEnum):
    """How a persona's member strategies relate to each other.

    COOPERATIVE — the members are legs of one portfolio (All Weather's five
    sleeves). They must ALL trade together; letting the allocator pick one
    would hold a single leg and silently shadow the rest.

    COMPETING — the members are alternative approaches. The allocator selects
    among them per regime, exactly as before personas existed.
    """

    COOPERATIVE = "cooperative"
    COMPETING = "competing"


class PersonaStatus(StrEnum):
    INACTIVE = "inactive"  # members are shadow-only
    ACTIVE = "active"  # members participate per the persona's kind
    RETIRED = "retired"


class ExperimentKind(StrEnum):
    SHADOW = "shadow"
    CHAMPION_CHALLENGER = "champion_challenger"
    BACKTEST = "backtest"


class ExperimentStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    ABORTED = "aborted"


# ---------------------------------------------------------------- market data


class MarketBar(Base):
    """OHLCV bar. Unique per (symbol, timeframe, ts, source)."""

    __tablename__ = "market_bars"
    __table_args__ = (
        sa.UniqueConstraint("symbol", "timeframe", "ts", "source", name="uq_bar"),
        sa.Index("ix_bars_symbol_tf_ts", "symbol", "timeframe", "ts"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger().with_variant(sa.Integer, "sqlite"),
                                    primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    timeframe: Mapped[str] = mapped_column(sa.String(8), nullable=False)  # e.g. 1Min, 1Day
    ts: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(NUMERIC, nullable=False)
    high: Mapped[Decimal] = mapped_column(NUMERIC, nullable=False)
    low: Mapped[Decimal] = mapped_column(NUMERIC, nullable=False)
    close: Mapped[Decimal] = mapped_column(NUMERIC, nullable=False)
    volume: Mapped[Decimal] = mapped_column(NUMERIC, nullable=False, default=0)
    source: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="alpaca")


# ---------------------------------------------------------------- signals


class Signal(TimestampedBase):
    __tablename__ = "signals"
    __table_args__ = (
        sa.UniqueConstraint("source", "dedupe_key", name="uq_signal_dedupe"),
        sa.Index("ix_signals_symbol_created", "symbol", "created_at"),
    )

    source: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("strategies.id"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    action: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=SignalStatus.RECEIVED
    )
    status_reason: Mapped[str | None] = mapped_column(sa.String(256))
    payload: Mapped[dict[str, Any]] = mapped_column(JsonDict, nullable=False, default=dict)
    signal_ts: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


# ---------------------------------------------------------------- strategies & models


class Strategy(TimestampedBase):
    """A versioned strategy configuration. (name, version) is immutable once
    created; changing parameters means registering a new version."""

    __tablename__ = "strategies"
    __table_args__ = (sa.UniqueConstraint("name", "version", name="uq_strategy_version"),)

    name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    class_path: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JsonDict, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=StrategyStatus.CANDIDATE
    )
    description: Mapped[str | None] = mapped_column(sa.Text)
    approved_by: Mapped[str | None] = mapped_column(sa.String(128))
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ModelVersion(TimestampedBase):
    """A versioned, immutable learning artifact (e.g. strategy-allocator
    weights). Promotion between statuses is a human admin action, audited in
    ``promotion_events``."""

    __tablename__ = "model_versions"
    __table_args__ = (sa.UniqueConstraint("name", "version", name="uq_model_version"),)

    name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    artifact: Mapped[dict[str, Any]] = mapped_column(JsonDict, nullable=False, default=dict)
    training_meta: Mapped[dict[str, Any]] = mapped_column(JsonDict, nullable=False, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JsonDict, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=ModelStatus.REGISTERED
    )


class PromotionEvent(TimestampedBase):
    """Audit trail for every strategy/model status change."""

    __tablename__ = "promotion_events"

    subject_type: Mapped[str] = mapped_column(sa.String(16), nullable=False)  # strategy|model
    subject_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False)
    from_status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    to_status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    actor: Mapped[str] = mapped_column(sa.String(128), nullable=False)  # human identifier
    reason: Mapped[str | None] = mapped_column(sa.Text)


# ---------------------------------------------------------------- decisions


class Decision(TimestampedBase):
    """The audit spine: one row per proposed trade, with full context.

    ``context`` holds everything needed to retrain: feature vector, market
    regime label, recent bars summary, account snapshot, allocator scores.
    ``outcome`` is filled in later (realized PnL, holding period, etc.).
    """

    __tablename__ = "decisions"
    __table_args__ = (
        sa.Index("ix_decisions_strategy_created", "strategy_id", "created_at"),
        sa.Index("ix_decisions_mode_status", "mode", "status"),
    )

    mode: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    environment: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=DecisionStatus.PROPOSED
    )
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, sa.ForeignKey("strategies.id"))
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("model_versions.id")
    )
    signal_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, sa.ForeignKey("signals.id"))
    symbol: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    action: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    qty: Mapped[Decimal] = mapped_column(NUMERIC, nullable=False)
    order_type: Mapped[str] = mapped_column(sa.String(16), nullable=False, default=OrderType.MARKET)
    limit_price: Mapped[Decimal | None] = mapped_column(NUMERIC)
    context: Mapped[dict[str, Any]] = mapped_column(JsonDict, nullable=False, default=dict)
    risk_verdict: Mapped[str | None] = mapped_column(sa.String(16))
    risk_detail: Mapped[dict[str, Any]] = mapped_column(JsonDict, nullable=False, default=dict)
    outcome: Mapped[dict[str, Any]] = mapped_column(JsonDict, nullable=False, default=dict)
    outcome_recorded_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    orders: Mapped[list[Order]] = relationship(back_populates="decision")


# ---------------------------------------------------------------- orders & fills


class Order(TimestampedBase):
    __tablename__ = "orders"
    __table_args__ = (
        sa.UniqueConstraint("client_order_id", name="uq_client_order_id"),
        sa.Index("ix_orders_env_status", "environment", "status"),
        sa.Index("ix_orders_symbol_created", "symbol", "created_at"),
    )

    decision_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, sa.ForeignKey("decisions.id"))
    client_order_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    environment: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    side: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    qty: Mapped[Decimal] = mapped_column(NUMERIC, nullable=False)
    order_type: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(NUMERIC)
    time_in_force: Mapped[str] = mapped_column(sa.String(8), nullable=False, default="day")
    status: Mapped[str] = mapped_column(
        sa.String(24), nullable=False, default=OrderStatus.PENDING_SUBMIT
    )
    status_reason: Mapped[str | None] = mapped_column(sa.String(512))
    filled_qty: Mapped[Decimal] = mapped_column(NUMERIC, nullable=False, default=0)
    filled_avg_price: Mapped[Decimal | None] = mapped_column(NUMERIC)
    submitted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    raw_broker_payload: Mapped[dict[str, Any]] = mapped_column(
        JsonDict, nullable=False, default=dict
    )

    decision: Mapped[Decision | None] = relationship(back_populates="orders")
    fills: Mapped[list[Fill]] = relationship(back_populates="order")


class Fill(TimestampedBase):
    __tablename__ = "fills"
    __table_args__ = (
        sa.UniqueConstraint("order_id", "broker_fill_id", name="uq_fill_dedupe"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("orders.id"), nullable=False
    )
    broker_fill_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    qty: Mapped[Decimal] = mapped_column(NUMERIC, nullable=False)
    price: Mapped[Decimal] = mapped_column(NUMERIC, nullable=False)
    filled_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    order: Mapped[Order] = relationship(back_populates="fills")


class PositionSnapshot(TimestampedBase):
    """Periodic snapshot of broker-reported positions and account equity,
    used for risk (drawdown, exposure) and reconciliation."""

    __tablename__ = "position_snapshots"
    __table_args__ = (sa.Index("ix_possnap_env_created", "environment", "created_at"),)

    environment: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    equity: Mapped[Decimal] = mapped_column(NUMERIC, nullable=False)
    cash: Mapped[Decimal] = mapped_column(NUMERIC, nullable=False)
    positions: Mapped[list[dict[str, Any]]] = mapped_column(JsonDict, nullable=False, default=list)


# ---------------------------------------------------------------- risk


class RiskEvent(TimestampedBase):
    """Every risk-engine evaluation outcome and every state change (breaker
    trip, kill-switch flip) is recorded here."""

    __tablename__ = "risk_events"
    __table_args__ = (sa.Index("ix_risk_events_rule_created", "rule", "created_at"),)

    rule: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    severity: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="info")
    environment: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, sa.ForeignKey("decisions.id"))
    detail: Mapped[dict[str, Any]] = mapped_column(JsonDict, nullable=False, default=dict)


class SystemControl(Base):
    """Operator-controlled runtime switches (kill switch, live arming,
    circuit-breaker state). Key-value with audit fields; changes go through
    the admin API only."""

    __tablename__ = "system_controls"

    key: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JsonDict, nullable=False, default=dict)
    updated_by: Mapped[str | None] = mapped_column(sa.String(128))
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


# Well-known SystemControl keys.
CONTROL_KILL_SWITCH = "kill_switch"  # {"engaged": bool, "reason": str}
CONTROL_LIVE_ARMED = "live_trading_armed"  # {"armed": bool}
CONTROL_CIRCUIT_BREAKER = "circuit_breaker"  # {"tripped": bool, "until": iso ts, "reason": str}


# ---------------------------------------------------------------- performance & experiments


class PerformanceReport(TimestampedBase):
    """Aggregated performance of a strategy (optionally under a specific
    model and market regime) over a period. Written by evaluation jobs and
    the backtester; read by the allocator trainer."""

    __tablename__ = "performance_reports"
    __table_args__ = (
        sa.Index("ix_perf_strategy_period", "strategy_id", "period_start", "period_end"),
    )

    strategy_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("strategies.id"), nullable=False
    )
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("model_versions.id")
    )
    environment: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    mode: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    regime: Mapped[str | None] = mapped_column(sa.String(32))
    period_start: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JsonDict, nullable=False, default=dict)


class Persona(TimestampedBase):
    """A named bundle of strategy versions — "run the platform as Dalio".

    A persona is a *preset, not a privilege*: its members still produce
    ordinary TradeIntents that pass through the same risk engine and audit
    spine. What a persona changes is only *which* approved strategies trade
    together and how their modes are assigned (see ``PersonaKind``).
    """

    __tablename__ = "personas"
    __table_args__ = (sa.UniqueConstraint("name", name="uq_persona_name"),)

    name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text)
    #: Honest statement of how faithfully this reproduces the original method.
    fidelity_note: Mapped[str | None] = mapped_column(sa.Text)
    kind: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=PersonaKind.COMPETING
    )
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=PersonaStatus.INACTIVE
    )
    #: Symbols the persona is designed for; informational — the risk engine's
    #: allowlist remains the enforcing authority.
    symbol_set: Mapped[list[str]] = mapped_column(JsonDict, nullable=False, default=list)
    activated_by: Mapped[str | None] = mapped_column(sa.String(128))
    activated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    members: Mapped[list[PersonaMember]] = relationship(
        back_populates="persona", cascade="all, delete-orphan"
    )


class PersonaMember(TimestampedBase):
    """Membership of one strategy version in a persona, with its weight."""

    __tablename__ = "persona_members"
    __table_args__ = (
        sa.UniqueConstraint("persona_id", "strategy_id", name="uq_persona_member"),
    )

    persona_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("personas.id"), nullable=False
    )
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("strategies.id"), nullable=False
    )
    #: Relative weight within the persona. Feeds sizing only — never risk checks.
    weight: Mapped[Decimal] = mapped_column(NUMERIC, nullable=False, default=1)

    persona: Mapped[Persona] = relationship(back_populates="members")


class Experiment(TimestampedBase):
    """A shadow test or champion-vs-challenger comparison. Results include a
    recommendation; promotion itself is always a separate human action."""

    __tablename__ = "experiments"

    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, default=ExperimentStatus.RUNNING
    )
    champion_model_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("model_versions.id")
    )
    challenger_model_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid, sa.ForeignKey("model_versions.id")
    )
    config: Mapped[dict[str, Any]] = mapped_column(JsonDict, nullable=False, default=dict)
    results: Mapped[dict[str, Any]] = mapped_column(JsonDict, nullable=False, default=dict)
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
