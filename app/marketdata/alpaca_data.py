"""Alpaca market-data provider (data API v2) and bar persistence.

Reuses the transport/parse helpers from the broker adapter. The data host is
a hardcoded constant like the trading hosts; paper credentials work for
market data, so this always authenticates with whichever key pair the caller
supplies (typically paper).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.alpaca import (
    DEFAULT_TIMEOUT_SECONDS,
    parse_timestamp,
    raise_for_client_error,
    request_with_retry,
    to_decimal,
)
from app.brokers.base import MarketDataProvider
from app.db.models import MarketBar
from app.logging import get_logger
from app.schemas.core import BarData

log = get_logger("marketdata.alpaca")

DATA_URL = "https://data.alpaca.markets"
PAGE_LIMIT = 10_000


def _parse_bar(symbol: str, timeframe: str, item: dict[str, Any]) -> BarData | None:
    ts = parse_timestamp(item.get("t"))
    if ts is None:
        return None
    return BarData(
        symbol=symbol,
        timeframe=timeframe,
        ts=ts,
        open=to_decimal(item.get("o")) or Decimal(0),
        high=to_decimal(item.get("h")) or Decimal(0),
        low=to_decimal(item.get("l")) or Decimal(0),
        close=to_decimal(item.get("c")) or Decimal(0),
        volume=to_decimal(item.get("v")) or Decimal(0),
    )


class AlpacaDataProvider(MarketDataProvider):
    def __init__(
        self, api_key: str, api_secret: str, client: httpx.AsyncClient | None = None
    ) -> None:
        self._headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)

    async def get_bars(
        self, symbol: str, timeframe: str, start: datetime, end: datetime
    ) -> list[BarData]:
        bars: list[BarData] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "timeframe": timeframe,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": PAGE_LIMIT,
                "adjustment": "raw",
                "feed": "iex",
            }
            if page_token:
                params["page_token"] = page_token
            response = await request_with_retry(
                self._client,
                "GET",
                f"{DATA_URL}/v2/stocks/{symbol}/bars",
                headers=self._headers,
                params=params,
            )
            raise_for_client_error(response)
            data = response.json()
            for item in data.get("bars") or []:
                bar = _parse_bar(symbol, timeframe, item)
                if bar is not None:
                    bars.append(bar)
            page_token = data.get("next_page_token")
            if not page_token:
                return bars

    async def get_latest_bar(self, symbol: str, timeframe: str) -> BarData | None:
        response = await request_with_retry(
            self._client,
            "GET",
            f"{DATA_URL}/v2/stocks/{symbol}/bars/latest",
            headers=self._headers,
            params={"feed": "iex"},
        )
        if response.status_code == 404:
            return None
        raise_for_client_error(response)
        item = response.json().get("bar")
        if not item:
            return None
        return _parse_bar(symbol, timeframe, item)


async def store_bars(
    session: AsyncSession, bars: list[BarData], source: str = "alpaca"
) -> int:
    """Persist bars, skipping (symbol, timeframe, ts, source) rows that already
    exist. Portable across PostgreSQL and SQLite (no dialect upserts)."""
    if not bars:
        return 0
    symbols = {b.symbol for b in bars}
    timeframes = {b.timeframe for b in bars}
    existing = (
        await session.execute(
            sa.select(MarketBar.symbol, MarketBar.timeframe, MarketBar.ts).where(
                MarketBar.symbol.in_(symbols),
                MarketBar.timeframe.in_(timeframes),
                MarketBar.source == source,
                MarketBar.ts.in_([b.ts for b in bars]),
            )
        )
    ).all()
    seen = {(r.symbol, r.timeframe, _naive(r.ts)) for r in existing}
    inserted = 0
    for bar in bars:
        key = (bar.symbol, bar.timeframe, _naive(bar.ts))
        if key in seen:
            continue
        seen.add(key)
        session.add(
            MarketBar(
                symbol=bar.symbol,
                timeframe=bar.timeframe,
                ts=bar.ts,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                source=source,
            )
        )
        inserted += 1
    await session.flush()
    return inserted


def _naive(ts: datetime) -> datetime:
    """Comparison key tolerant of SQLite's tz-dropping round trip."""
    return ts.replace(tzinfo=None)
