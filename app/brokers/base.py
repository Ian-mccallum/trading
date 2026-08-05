"""Broker abstraction. Every broker (Alpaca paper, Alpaca live, backtest
simulator) implements this interface; execution code never talks to a broker
SDK or HTTP API directly.

Adapters are environment-locked at construction: an adapter instance is
created for exactly one ``Environment`` and cannot be repointed.
"""

from __future__ import annotations

import abc
from datetime import datetime

from app.db.models import Environment
from app.schemas.core import (
    AccountState,
    BarData,
    FillData,
    OrderRequest,
    OrderState,
    PositionState,
)


class BrokerError(Exception):
    """Base class for broker failures."""


class BrokerRejectionError(BrokerError):
    """The broker actively rejected the request (4xx-style)."""


class BrokerUnavailableError(BrokerError):
    """Transport/5xx-style failure; the request may or may not have landed."""


class Broker(abc.ABC):
    """Order execution + account state for one environment."""

    environment: Environment

    @abc.abstractmethod
    async def get_account(self) -> AccountState: ...

    @abc.abstractmethod
    async def get_positions(self) -> list[PositionState]: ...

    @abc.abstractmethod
    async def submit_order(self, request: OrderRequest) -> OrderState:
        """Submit an order. MUST be idempotent on ``client_order_id``:
        resubmitting the same id must not create a second order."""

    @abc.abstractmethod
    async def get_order(self, client_order_id: str) -> OrderState | None: ...

    @abc.abstractmethod
    async def cancel_order(self, client_order_id: str) -> None: ...

    @abc.abstractmethod
    async def list_open_orders(self) -> list[OrderState]: ...

    @abc.abstractmethod
    async def list_fills(self, since: datetime) -> list[FillData]: ...


class MarketDataProvider(abc.ABC):
    """Historical/latest bar access, independent of order execution."""

    @abc.abstractmethod
    async def get_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[BarData]: ...

    @abc.abstractmethod
    async def get_latest_bar(self, symbol: str, timeframe: str) -> BarData | None: ...
