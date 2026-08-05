"""Liveness/readiness."""

from __future__ import annotations

from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.base import get_db_session

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    await session.execute(sa.text("SELECT 1"))
    return {
        "status": "ok",
        "app": settings.app_name,
        "trading_env": settings.trading_env,
        "live_trading_config_gates": settings.live_trading_allowed(),
    }
