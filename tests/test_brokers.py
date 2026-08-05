"""Alpaca broker adapter and market-data provider tests (respx-mocked HTTP)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from app.brokers.alpaca import (
    LIVE_TRADING_URL,
    PAPER_TRADING_URL,
    AlpacaBroker,
    make_alpaca_broker,
    map_order_status,
)
from app.brokers.base import BrokerRejectionError, BrokerUnavailableError
from app.config import LIVE_CONFIRMATION_PHRASE, Settings
from app.db.models import Environment, OrderStatus
from app.marketdata.alpaca_data import DATA_URL, AlpacaDataProvider, store_bars
from app.schemas.core import BarData, OrderRequest, OrderSide


def paper_broker() -> AlpacaBroker:
    return AlpacaBroker(Environment.PAPER, "key", "secret")


ORDER_JSON = {
    "id": "broker-1",
    "client_order_id": "qp-abc",
    "symbol": "SPY",
    "side": "buy",
    "qty": "5",
    "type": "market",
    "status": "accepted",
    "filled_qty": "0",
    "filled_avg_price": None,
    "submitted_at": "2026-07-18T14:30:00.123456789Z",
}


@respx.mock
async def test_get_account_parses_paper_host():
    route = respx.get(f"{PAPER_TRADING_URL}/v2/account").mock(
        return_value=httpx.Response(
            200, json={"equity": "100000.50", "cash": "50000", "buying_power": "200000"}
        )
    )
    account = await paper_broker().get_account()
    assert route.called
    assert account.equity == Decimal("100000.50")
    assert account.cash == Decimal("50000")
    assert account.environment == Environment.PAPER


@respx.mock
async def test_live_broker_uses_live_host():
    route = respx.get(f"{LIVE_TRADING_URL}/v2/account").mock(
        return_value=httpx.Response(
            200, json={"equity": "1", "cash": "1", "buying_power": "1"}
        )
    )
    broker = AlpacaBroker(Environment.LIVE, "key", "secret")
    await broker.get_account()
    assert route.called


@respx.mock
async def test_submit_order_success():
    respx.post(f"{PAPER_TRADING_URL}/v2/orders").mock(
        return_value=httpx.Response(200, json=ORDER_JSON)
    )
    state = await paper_broker().submit_order(
        OrderRequest(client_order_id="qp-abc", symbol="SPY", side=OrderSide.BUY, qty=Decimal(5))
    )
    assert state.broker_order_id == "broker-1"
    assert state.status == OrderStatus.ACCEPTED
    assert state.submitted_at == datetime(2026, 7, 18, 14, 30, 0, 123456, tzinfo=UTC)


@respx.mock
async def test_submit_duplicate_client_order_id_returns_existing():
    respx.post(f"{PAPER_TRADING_URL}/v2/orders").mock(
        return_value=httpx.Response(
            422, json={"code": 40010001, "message": "client_order_id must be unique"}
        )
    )
    respx.get(f"{PAPER_TRADING_URL}/v2/orders:by_client_order_id").mock(
        return_value=httpx.Response(200, json=ORDER_JSON)
    )
    state = await paper_broker().submit_order(
        OrderRequest(client_order_id="qp-abc", symbol="SPY", side=OrderSide.BUY, qty=Decimal(5))
    )
    assert state.broker_order_id == "broker-1"  # resolved, not raised


@respx.mock
async def test_submit_4xx_raises_rejection():
    respx.post(f"{PAPER_TRADING_URL}/v2/orders").mock(
        return_value=httpx.Response(403, json={"message": "insufficient buying power"})
    )
    with pytest.raises(BrokerRejectionError, match="insufficient buying power"):
        await paper_broker().submit_order(
            OrderRequest(client_order_id="x", symbol="SPY", side=OrderSide.BUY, qty=Decimal(5))
        )


@respx.mock
async def test_submit_5xx_raises_unavailable_without_retry():
    route = respx.post(f"{PAPER_TRADING_URL}/v2/orders").mock(
        return_value=httpx.Response(500)
    )
    with pytest.raises(BrokerUnavailableError):
        await paper_broker().submit_order(
            OrderRequest(client_order_id="x", symbol="SPY", side=OrderSide.BUY, qty=Decimal(5))
        )
    assert route.call_count == 1  # POSTs are never auto-retried


@respx.mock
async def test_get_retries_then_succeeds():
    route = respx.get(f"{PAPER_TRADING_URL}/v2/account")
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(200, json={"equity": "1", "cash": "1", "buying_power": "1"}),
    ]
    account = await paper_broker().get_account()
    assert route.call_count == 2
    assert account.equity == Decimal(1)


@respx.mock
async def test_get_order_404_returns_none():
    respx.get(f"{PAPER_TRADING_URL}/v2/orders:by_client_order_id").mock(
        return_value=httpx.Response(404, json={"message": "order not found"})
    )
    assert await paper_broker().get_order("missing") is None


@respx.mock
async def test_list_fills_maps_activities():
    respx.get(f"{PAPER_TRADING_URL}/v2/account/activities/FILL").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "act-1",
                    "order_id": "broker-1",
                    "qty": "5",
                    "price": "100.25",
                    "transaction_time": "2026-07-18T14:31:00Z",
                }
            ],
        )
    )
    fills = await paper_broker().list_fills(since=datetime(2026, 7, 18, tzinfo=UTC))
    assert len(fills) == 1
    assert fills[0].broker_order_id == "broker-1"
    assert fills[0].client_order_id is None
    assert fills[0].price == Decimal("100.25")


def test_status_mapping():
    assert map_order_status("new") == OrderStatus.ACCEPTED
    assert map_order_status("partially_filled") == OrderStatus.PARTIALLY_FILLED
    assert map_order_status("filled") == OrderStatus.FILLED
    assert map_order_status("canceled") == OrderStatus.CANCELED
    assert map_order_status("expired") == OrderStatus.EXPIRED
    assert map_order_status("rejected") == OrderStatus.REJECTED
    assert map_order_status("weird_future_status") == OrderStatus.SUBMITTED


def test_factory_paper_ok_live_refused():
    paper = make_alpaca_broker(
        Settings(_env_file=None, alpaca_paper_api_key="k", alpaca_paper_api_secret="s")
    )
    assert paper.environment == Environment.PAPER

    with pytest.raises(RuntimeError, match="live-trading gates"):
        make_alpaca_broker(Settings(_env_file=None, trading_env="live"))


def test_factory_live_allowed_only_fully_gated():
    broker = make_alpaca_broker(
        Settings(
            _env_file=None,
            trading_env="live",
            live_trading_enabled=True,
            live_trading_confirmation=LIVE_CONFIRMATION_PHRASE,
            alpaca_live_api_key="lk",
            alpaca_live_api_secret="ls",
        )
    )
    assert broker.environment == Environment.LIVE


# ---------------------------------------------------------------- market data


@respx.mock
async def test_get_bars_paginates():
    route = respx.get(f"{DATA_URL}/v2/stocks/SPY/bars")
    route.side_effect = [
        httpx.Response(
            200,
            json={
                "bars": [
                    {"t": "2026-07-16T20:00:00Z", "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000}
                ],
                "next_page_token": "tok",
            },
        ),
        httpx.Response(
            200,
            json={
                "bars": [
                    {"t": "2026-07-17T20:00:00Z", "o": 100.5, "h": 102, "l": 100, "c": 101, "v": 900}
                ],
                "next_page_token": None,
            },
        ),
    ]
    provider = AlpacaDataProvider("k", "s")
    bars = await provider.get_bars(
        "SPY", "1Day", datetime(2026, 7, 16, tzinfo=UTC), datetime(2026, 7, 18, tzinfo=UTC)
    )
    assert route.call_count == 2
    assert [b.close for b in bars] == [Decimal("100.5"), Decimal("101")]
    assert bars[0].symbol == "SPY"


@respx.mock
async def test_get_latest_bar():
    respx.get(f"{DATA_URL}/v2/stocks/SPY/bars/latest").mock(
        return_value=httpx.Response(
            200,
            json={"bar": {"t": "2026-07-18T14:30:00Z", "o": 1, "h": 2, "l": 1, "c": 1.5, "v": 10}},
        )
    )
    bar = await AlpacaDataProvider("k", "s").get_latest_bar("SPY", "1Day")
    assert bar is not None
    assert bar.close == Decimal("1.5")


async def test_store_bars_dedupes(db_session):
    bars = [
        BarData(
            symbol="SPY", timeframe="1Day", ts=datetime(2026, 7, 16, 20, tzinfo=UTC),
            open=Decimal(100), high=Decimal(101), low=Decimal(99), close=Decimal(100),
        ),
        BarData(
            symbol="SPY", timeframe="1Day", ts=datetime(2026, 7, 17, 20, tzinfo=UTC),
            open=Decimal(100), high=Decimal(102), low=Decimal(100), close=Decimal(101),
        ),
    ]
    assert await store_bars(db_session, bars) == 2
    await db_session.commit()
    # Second call with an overlapping batch inserts only the new bar.
    more = bars + [
        BarData(
            symbol="SPY", timeframe="1Day", ts=datetime(2026, 7, 18, 20, tzinfo=UTC),
            open=Decimal(101), high=Decimal(103), low=Decimal(101), close=Decimal(102),
        )
    ]
    assert await store_bars(db_session, more) == 1
