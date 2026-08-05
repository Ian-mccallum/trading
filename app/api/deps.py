"""Shared API dependencies."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from app.brokers.base import Broker
from app.config import Settings, get_settings
from app.logging import get_logger

log = get_logger("api.deps")


def require_admin(
    settings: Annotated[Settings, Depends(get_settings)],
    x_admin_token: Annotated[str | None, Header()] = None,
) -> None:
    """All /admin endpoints require the configured token. An empty configured
    token disables the admin API outright (403 for everyone)."""
    if not settings.admin_api_token:
        raise HTTPException(status_code=403, detail="admin API disabled")
    if not x_admin_token or not hmac.compare_digest(
        x_admin_token, settings.admin_api_token
    ):
        raise HTTPException(status_code=401, detail="invalid admin token")


def build_broker(settings: Settings) -> Broker | None:
    """Construct a broker for the active environment, or None if unconfigured.

    Returning None rather than raising keeps read-only surfaces useful without
    credentials. Callers that actually need to trade must turn None into an
    explicit error rather than silently doing nothing.
    """
    if settings.trading_env == "live" and not settings.live_trading_allowed():
        return None
    key = (
        settings.alpaca_live_api_key
        if settings.trading_env == "live"
        else settings.alpaca_paper_api_key
    )
    if not key:
        return None
    from app.brokers.alpaca import make_alpaca_broker

    try:
        return make_alpaca_broker(settings)
    except RuntimeError as exc:  # ungated live config
        log.warning("broker_unavailable", error=str(exc))
        return None


async def get_execution_service(
    settings: Annotated[Settings, Depends(get_settings)],
):
    """Execution service for the admin API's manual-order endpoints.

    Deliberately the *same* ExecutionService the worker uses, so a manual
    order takes the identical path: Decision row, risk engine, order
    lifecycle. There is no lighter-weight "just place it" route, because that
    is exactly the bypass this endpoint must not become.
    """
    from app.execution.service import ExecutionService
    from app.risk.engine import RiskEngine

    broker = build_broker(settings)
    if broker is None:
        return None
    try:
        return ExecutionService(broker, RiskEngine(settings), settings)
    except RuntimeError as exc:  # broker/env mismatch
        log.error("execution_service_unavailable", error=str(exc))
        await broker.aclose()
        return None
