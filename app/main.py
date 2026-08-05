"""FastAPI application wiring.

The API layer does ingestion (webhooks), health, and the operator/admin
surface. All trading activity happens in the worker process; the API's only
path into trading is persisting a validated Signal and enqueuing its id.
"""

from __future__ import annotations

import contextlib
import uuid

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI

from app.api.routes import admin, dashboard, health, webhooks
from app.api.routes.webhooks import get_signal_enqueuer
from app.config import get_settings
from app.logging import configure_logging, get_logger

log = get_logger("api.main")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.debug)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.arq_pool = await create_pool(
            RedisSettings.from_dsn(settings.redis_url)
        )
        log.info(
            "api_started",
            trading_env=settings.trading_env,
            live_config_gates=settings.live_trading_allowed(),
        )
        yield
        await app.state.arq_pool.aclose()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(webhooks.router)
    app.include_router(admin.router)
    app.include_router(dashboard.router)

    async def arq_enqueuer():
        async def enqueue(signal_id: uuid.UUID) -> None:
            await app.state.arq_pool.enqueue_job(
                "process_signal",
                str(signal_id),
                _job_id=f"signal-{signal_id}",  # idempotent enqueue per signal
            )

        return enqueue

    app.dependency_overrides[get_signal_enqueuer] = arq_enqueuer
    return app


app = create_app()
