"""Shared test fixtures.

Unit tests run against in-memory SQLite (aiosqlite); the ORM models are
written to be portable. Integration against real Postgres happens via
docker compose, not in the default pytest run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Ensure every model is registered on Base.metadata before create_all.
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models import Environment
from app.schemas.core import AccountState, BarData, PositionState


@pytest.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
def session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False)


# ---------------------------------------------------------------- helpers


def make_bars(
    symbol: str = "SPY",
    n: int = 50,
    start_price: float = 100.0,
    step: float = 1.0,
    timeframe: str = "1Day",
    start: datetime | None = None,
) -> list[BarData]:
    """Deterministic ascending/descending bar series for strategy tests."""
    start = start or datetime(2024, 1, 1, tzinfo=UTC)
    bars = []
    price = Decimal(str(start_price))
    delta = Decimal(str(step))
    for i in range(n):
        o = price
        c = price + delta
        bars.append(
            BarData(
                symbol=symbol,
                timeframe=timeframe,
                ts=start + timedelta(days=i),
                open=o,
                high=max(o, c) + Decimal("0.5"),
                low=min(o, c) - Decimal("0.5"),
                close=c,
                volume=Decimal(1000),
            )
        )
        price = c
    return bars


def make_account(equity: str = "100000", env: Environment = Environment.PAPER) -> AccountState:
    eq = Decimal(equity)
    return AccountState(equity=eq, cash=eq, buying_power=eq, environment=env)


def make_position(symbol: str = "SPY", qty: str = "10", price: str = "100") -> PositionState:
    q, p = Decimal(qty), Decimal(price)
    return PositionState(
        symbol=symbol,
        qty=q,
        avg_entry_price=p,
        market_value=q * p,
        unrealized_pl=Decimal(0),
    )


def new_id() -> uuid.UUID:
    return uuid.uuid4()
