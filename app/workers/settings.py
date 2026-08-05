"""arq worker configuration: queue functions + cron schedule.

The worker owns the broker connection and the execution service. It is the
only process that submits orders.
"""

from __future__ import annotations

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.logging import configure_logging, get_logger
from app.workers import tasks

log = get_logger("workers")


async def startup(ctx: dict) -> None:
    from app.brokers.alpaca import make_alpaca_broker
    from app.execution.service import ExecutionService
    from app.marketdata.alpaca_data import AlpacaDataProvider
    from app.risk.engine import RiskEngine

    settings = get_settings()
    configure_logging(settings.debug)
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    ctx["engine"] = engine
    ctx["settings"] = settings
    ctx["session_factory"] = async_sessionmaker(engine, expire_on_commit=False)
    broker = make_alpaca_broker(settings)
    ctx["broker"] = broker
    ctx["data_provider"] = AlpacaDataProvider(
        settings.alpaca_paper_api_key, settings.alpaca_paper_api_secret
    )
    ctx["execution"] = ExecutionService(broker, RiskEngine(settings), settings)
    log.info("worker_started", trading_env=settings.trading_env)


async def shutdown(ctx: dict) -> None:
    await ctx["engine"].dispose()


class WorkerSettings:
    functions = [tasks.process_signal]
    cron_jobs = [
        cron(tasks.refresh_market_data, minute=set(range(0, 60, 5)), run_at_startup=True),
        cron(tasks.run_strategies, minute=set(range(2, 60, 15))),
        cron(tasks.sync_orders, minute=set(range(0, 60))),
        cron(tasks.reconcile_orders, minute=set(range(3, 60, 10))),
        cron(tasks.snapshot_positions, minute=set(range(1, 60, 15))),
        cron(tasks.evaluate_outcomes, minute={10}),
        cron(tasks.train_allocator_job, hour={3}, minute={0}),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
