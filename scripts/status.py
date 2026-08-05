"""One-screen view of what the platform is doing right now.

Answers "is it running, and what has it done?" without needing to remember
admin curl invocations or SQL. Read-only.

Usage: .venv/bin/python -m scripts.status
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.brokers.alpaca import make_alpaca_broker
from app.config import get_settings
from app.db.base import utcnow
from app.db.models import (
    Decision,
    Order,
    OrderStatus,
    Persona,
    PersonaStatus,
    RiskEvent,
    Strategy,
    StrategyStatus,
)
from app.risk import state as controls


async def main() -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = utcnow()

    async with factory() as s:
        print("\n═══ CONTROLS ═══")
        engaged, ks_reason = await controls.kill_switch_engaged(s)
        tripped, cb_reason = await controls.circuit_breaker_tripped(s, now)
        armed = await controls.live_trading_armed(s)
        print(f"  environment    : {settings.trading_env}")
        print(f"  kill switch    : {'ENGAGED — ' + ks_reason if engaged else 'off'}")
        print(f"  circuit breaker: {'TRIPPED — ' + cb_reason if tripped else 'ok'}")
        print(f"  live trading   : gates={settings.live_trading_allowed()} armed={armed}")

        print("\n═══ PERSONAS ═══")
        personas = (await s.execute(sa.select(Persona).order_by(Persona.name))).scalars().all()
        for p in personas:
            mark = "▶" if p.status == PersonaStatus.ACTIVE else " "
            print(f"  {mark} {p.name:22} {p.kind:12} {p.status}")
        if not personas:
            print("  (none — run scripts.seed_personas)")

        approved = (
            await s.execute(
                sa.select(sa.func.count(Strategy.id)).where(
                    Strategy.status.in_([StrategyStatus.APPROVED, StrategyStatus.CHAMPION])
                )
            )
        ).scalar_one()
        total = (await s.execute(sa.select(sa.func.count(Strategy.id)))).scalar_one()
        print(f"  strategies: {approved} tradeable / {total} registered")

        print("\n═══ RECENT DECISIONS (24h) ═══")
        cutoff = now - timedelta(hours=24)
        rows = (
            await s.execute(
                sa.select(
                    Decision.mode, Decision.status, sa.func.count(Decision.id)
                ).where(Decision.created_at >= cutoff)
                .group_by(Decision.mode, Decision.status)
                .order_by(Decision.mode)
            )
        ).all()
        if rows:
            for mode, status, count in rows:
                print(f"  {mode:8} {status:10} {count:>4}")
        else:
            print("  (none yet — the strategy loop runs every 15 min)")

        print("\n═══ ORDERS ═══")
        orders = (
            await s.execute(sa.select(Order).order_by(Order.created_at.desc()).limit(8))
        ).scalars().all()
        if orders:
            for o in orders:
                filled = f" filled={o.filled_qty}@{o.filled_avg_price}" if o.filled_qty else ""
                print(f"  {o.created_at:%m-%d %H:%M} {o.symbol:5} {o.side:4} "
                      f"qty={o.qty:<6} {o.status}{filled}")
        else:
            print("  (none yet)")
        open_count = (
            await s.execute(
                sa.select(sa.func.count(Order.id)).where(
                    Order.status.notin_(list(OrderStatus.terminal()))
                )
            )
        ).scalar_one()
        print(f"  {open_count} still open/working")

        print("\n═══ RISK REJECTIONS (24h) ═══")
        rejections = (
            await s.execute(
                sa.select(RiskEvent.rule, sa.func.count(RiskEvent.id))
                .where(RiskEvent.created_at >= cutoff, RiskEvent.verdict != "approved")
                .group_by(RiskEvent.rule)
                .order_by(sa.func.count(RiskEvent.id).desc())
            )
        ).all()
        for rule, count in rejections or []:
            print(f"  {rule:22} {count:>4}")
        if not rejections:
            print("  (none)")

    await engine.dispose()

    print("\n═══ BROKER ═══")
    if not settings.alpaca_paper_api_key:
        print("  (no credentials configured)")
        return 0
    broker = make_alpaca_broker(settings)
    try:
        account = await broker.get_account()
        positions = await broker.get_positions()
        print(f"  equity ${account.equity}   cash ${account.cash}")
        if positions:
            for p in sorted(positions, key=lambda x: x.symbol):
                print(f"    {p.symbol:5} qty={p.qty:<6} value=${p.market_value:<12} "
                      f"pnl=${p.unrealized_pl}")
        else:
            print("  no open positions")
    finally:
        await broker.aclose()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
