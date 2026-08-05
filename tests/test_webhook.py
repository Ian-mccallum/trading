"""TradingView webhook ingestion tests: auth, redaction, freshness, dedupe,
normalization, and the enqueue hook."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from app.api.routes import webhooks as webhook_routes
from app.config import Settings, get_settings
from app.db.base import get_db_session
from app.db.models import Signal, SignalStatus

SECRET = "topsecret"


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, tradingview_webhook_secret=SECRET, **overrides)


@pytest.fixture
def enqueued():
    return []


@pytest.fixture
def app(session_factory, enqueued):
    app = FastAPI()
    app.include_router(webhook_routes.router)

    async def override_session():
        async with session_factory() as session:
            yield session

    async def override_enqueuer():
        async def enqueue(signal_id: uuid.UUID) -> None:
            enqueued.append(signal_id)

        return enqueue

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_settings] = lambda: make_settings()
    app.dependency_overrides[webhook_routes.get_signal_enqueuer] = override_enqueuer
    return app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def alert(**overrides) -> dict:
    payload = {"secret": SECRET, "symbol": "SPY", "action": "buy", "qty": 5}
    payload.update(overrides)
    return payload


async def all_signals(session_factory) -> list[Signal]:
    async with session_factory() as session:
        return list((await session.execute(sa.select(Signal))).scalars().all())


async def test_valid_alert_accepted(client, session_factory, enqueued):
    resp = await client.post("/webhooks/tradingview", json=alert(signal_id="sig-1"))
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "validated"

    rows = await all_signals(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == SignalStatus.VALIDATED
    assert row.symbol == "SPY"
    assert row.payload["secret"] == "[redacted]"
    assert SECRET not in json.dumps(row.payload)
    assert enqueued == [row.id]
    assert body["signal_id"] == str(row.id)


async def test_wrong_secret_rejected(client, session_factory, enqueued):
    resp = await client.post("/webhooks/tradingview", json=alert(secret="nope"))
    assert resp.status_code == 401
    rows = await all_signals(session_factory)
    assert len(rows) == 1
    assert rows[0].status == SignalStatus.REJECTED
    assert rows[0].status_reason == "auth_failed"
    assert rows[0].payload["secret"] == "[redacted]"
    assert enqueued == []


async def test_unconfigured_secret_refuses_everything(app, session_factory):
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None, tradingview_webhook_secret=""
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/webhooks/tradingview", json=alert())
    assert resp.status_code == 503
    assert await all_signals(session_factory) == []


async def test_malformed_json_not_persisted(client, session_factory):
    resp = await client.post(
        "/webhooks/tradingview", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 422
    assert await all_signals(session_factory) == []


async def test_missing_fields_not_persisted(client, session_factory):
    resp = await client.post("/webhooks/tradingview", json={"secret": SECRET})
    assert resp.status_code == 422
    assert await all_signals(session_factory) == []


async def test_stale_signal_expired(client, session_factory, enqueued):
    stale = (datetime.now(UTC) - timedelta(seconds=600)).isoformat()
    resp = await client.post("/webhooks/tradingview", json=alert(time=stale))
    assert resp.status_code == 422
    rows = await all_signals(session_factory)
    assert len(rows) == 1
    assert rows[0].status == SignalStatus.EXPIRED
    assert enqueued == []


async def test_duplicate_signal_id_returns_200_once(client, session_factory, enqueued):
    first = await client.post("/webhooks/tradingview", json=alert(signal_id="dup-1"))
    second = await client.post("/webhooks/tradingview", json=alert(signal_id="dup-1"))
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    rows = await all_signals(session_factory)
    assert len(rows) == 1
    assert len(enqueued) == 1  # duplicates are not re-enqueued


async def test_symbol_normalized(client, session_factory):
    resp = await client.post(
        "/webhooks/tradingview", json=alert(symbol="NASDAQ:AAPL", signal_id="n-1")
    )
    assert resp.status_code == 202
    rows = await all_signals(session_factory)
    assert rows[0].symbol == "AAPL"


async def test_hmac_required_when_configured(app, session_factory):
    hmac_secret = "hmac-secret"
    app.dependency_overrides[get_settings] = lambda: make_settings(
        webhook_hmac_secret=hmac_secret
    )
    transport = httpx.ASGITransport(app=app)
    body = json.dumps(alert(signal_id="h-1")).encode()
    good_sig = hmac.new(hmac_secret.encode(), body, hashlib.sha256).hexdigest()

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        missing = await c.post(
            "/webhooks/tradingview", content=body, headers={"Content-Type": "application/json"}
        )
        bad = await c.post(
            "/webhooks/tradingview",
            content=body,
            headers={"Content-Type": "application/json", "X-Signature": "0" * 64},
        )
        good = await c.post(
            "/webhooks/tradingview",
            content=body,
            headers={"Content-Type": "application/json", "X-Signature": good_sig},
        )
    assert missing.status_code == 401
    assert bad.status_code == 401
    assert good.status_code == 202
