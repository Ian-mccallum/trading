"""TradingView webhook endpoint.

Thin HTTP shell over ``app.webhooks.tradingview.validate_and_store``. On a
validated signal it calls the enqueue hook; ``app.main`` overrides
``get_signal_enqueuer`` with an arq-backed implementation, tests override it
with recorders. The response never echoes the payload back.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.base import get_db_session
from app.logging import get_logger
from app.webhooks.tradingview import validate_and_store

log = get_logger("api.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

Enqueuer = Callable[[uuid.UUID], Awaitable[None]]


async def get_signal_enqueuer() -> Enqueuer:
    """Default enqueuer: log-only no-op. Overridden in app.main with arq."""

    async def enqueue(signal_id: uuid.UUID) -> None:
        log.info("enqueue_noop", signal_id=str(signal_id))

    return enqueue


@router.post("/tradingview")
async def tradingview_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    enqueue: Annotated[Enqueuer, Depends(get_signal_enqueuer)],
) -> JSONResponse:
    raw_body = await request.body()
    signal, detail, status_code = await validate_and_store(
        session, settings, raw_body, request.headers
    )
    body: dict = {"status": detail}
    if signal is not None:
        body["signal_id"] = str(signal.id)
    if status_code == 202 and signal is not None:
        await enqueue(signal.id)
    return JSONResponse(status_code=status_code, content=body)
