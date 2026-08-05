"""Shared Pydantic schemas used across modules. These are the in-memory
counterparts of the ORM rows and the wire formats for broker adapters.

All quantities and prices are ``Decimal`` end to end.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import (
    DecisionMode,
    Environment,
    OrderSide,
    OrderStatus,
    OrderType,
    SignalAction,
)


class BarData(BaseModel):
    """A single OHLCV bar."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    timeframe: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal(0)


class AccountState(BaseModel):
    """Broker account snapshot."""

    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    environment: Environment


class PositionState(BaseModel):
    symbol: str
    qty: Decimal  # signed: negative = short
    avg_entry_price: Decimal
    market_value: Decimal
    unrealized_pl: Decimal


class OrderRequest(BaseModel):
    """What execution submits to a broker adapter."""

    client_order_id: str
    symbol: str
    side: OrderSide
    qty: Decimal = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    time_in_force: str = "day"


class OrderState(BaseModel):
    """Normalized broker order state."""

    client_order_id: str
    broker_order_id: str | None = None
    symbol: str
    side: OrderSide
    qty: Decimal
    order_type: OrderType
    status: OrderStatus
    filled_qty: Decimal = Decimal(0)
    filled_avg_price: Decimal | None = None
    submitted_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class FillData(BaseModel):
    broker_fill_id: str
    broker_order_id: str | None = None
    client_order_id: str | None = None
    qty: Decimal
    price: Decimal
    filled_at: datetime


class TradeIntent(BaseModel):
    """A proposed trade before risk evaluation. Produced by strategies and by
    validated webhook signals; consumed by the execution service, which runs
    it through the risk engine before anything reaches a broker."""

    symbol: str
    action: SignalAction
    qty: Decimal = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    mode: DecisionMode = DecisionMode.PAPER
    strategy_id: uuid.UUID | None = None
    model_version_id: uuid.UUID | None = None
    signal_id: uuid.UUID | None = None
    context: dict[str, Any] = Field(default_factory=dict)
