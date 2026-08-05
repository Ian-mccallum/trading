"""Operator dashboard routes.

Two endpoints with deliberately different auth postures:

- ``GET /dashboard`` and its static assets carry **no data**, so they need no
  token. The page is an empty shell until it authenticates.
- ``GET /dashboard/data`` returns everything and requires the admin token,
  passed as a header so it never lands in a URL, browser history, or a server
  access log.

The dashboard is strictly read-only. State-changing operations stay on
``/admin`` where they are audited with an actor and reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.brokers.base import Broker
from app.config import Settings, get_settings
from app.dashboard.service import DEFAULT_RANGE, RANGES, build_dashboard
from app.db.base import get_db_session
from app.logging import get_logger

log = get_logger("api.dashboard")

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

STATIC_DIR = Path(__file__).resolve().parents[2] / "dashboard" / "static"

#: Long-lived immutable assets would need hashed filenames; these are small and
#: change with deploys, so a short cache keeps refreshes honest during dev.
_ASSET_CACHE = "public, max-age=60"


async def get_dashboard_broker(
    settings: Annotated[Settings, Depends(get_settings)],
) -> Broker | None:
    """Broker for read-only account queries, or None when unconfigured.

    Returning None rather than raising keeps the dashboard useful without
    credentials: everything sourced from the database still renders, and
    holdings report why they are unavailable.
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
        log.warning("dashboard_broker_unavailable", error=str(exc))
        return None


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def dashboard_page() -> FileResponse:
    """The shell. Contains no data, so it needs no token."""
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@router.get("/static/{filename}", include_in_schema=False)
async def dashboard_asset(filename: str) -> FileResponse:
    """Serve CSS/JS. The filename is matched against a fixed allowlist rather
    than joined onto a path, so traversal is impossible by construction."""
    allowed = {"dashboard.css": "text/css", "dashboard.js": "text/javascript"}
    media_type = allowed.get(filename)
    if media_type is None:
        return FileResponse(STATIC_DIR / "index.html", status_code=404)
    return FileResponse(
        STATIC_DIR / filename,
        media_type=media_type,
        headers={"Cache-Control": _ASSET_CACHE},
    )


@router.get("/data", dependencies=[Depends(require_admin)])
async def dashboard_data(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    broker: Annotated[Broker | None, Depends(get_dashboard_broker)],
    range: Annotated[str, Query(pattern="^(1D|1W|1M|3M|1Y|ALL)$")] = DEFAULT_RANGE,
) -> JSONResponse:
    payload = await build_dashboard(session, settings, broker, range_key=range)
    try:
        return JSONResponse(payload)
    finally:
        aclose = getattr(broker, "aclose", None)
        if aclose is not None:
            await aclose()


__all__ = ["router", "RANGES"]
