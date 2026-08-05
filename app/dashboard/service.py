"""Assemble everything the dashboard renders, as plain JSON-ready dicts.

Two rules shape this module:

1. **Never let a degraded dependency blank the page.** The broker can be
   unreachable while the database is fine; holdings then report *unavailable*
   with the reason, which is different information from "no positions" and
   must not look the same.
2. **Translate here, not in the browser.** Rule names, statuses, and criteria
   become sentences on the server (see ``app.dashboard.copy``) so the client
   stays a renderer and the wording is unit-testable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.brokers.base import Broker, BrokerError
from app.config import Settings
from app.dashboard import copy
from app.dashboard.charts import build_allocation, build_line_chart, build_volume
from app.db.base import utcnow
from app.db.models import (
    Decision,
    DecisionStatus,
    Order,
    OrderStatus,
    Persona,
    PersonaKind,
    PersonaStatus,
    PositionSnapshot,
    RiskEvent,
    RiskVerdict,
    Strategy,
    StrategyStatus,
)
from app.logging import get_logger
from app.personas.service import load_bindings
from app.risk import state as controls

log = get_logger("dashboard.service")

RANGES: dict[str, timedelta | None] = {
    "1D": timedelta(days=1),
    "1W": timedelta(days=7),
    "1M": timedelta(days=30),
    "3M": timedelta(days=90),
    "1Y": timedelta(days=365),
    "ALL": None,
}
DEFAULT_RANGE = "1M"

#: A worker is considered stalled if nothing has touched these tables within
#: this window. Snapshots run every 15 minutes, so 40 covers a missed cycle.
HEARTBEAT_STALE_MINUTES = 40


def _f(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


async def _system(session: AsyncSession, settings: Settings, now: datetime) -> dict:
    engaged, ks_reason = await controls.kill_switch_engaged(session)
    tripped, cb_reason = await controls.circuit_breaker_tripped(session, now)
    armed = await controls.live_trading_armed(session)

    last_snapshot = (
        await session.execute(sa.select(sa.func.max(PositionSnapshot.created_at)))
    ).scalar_one_or_none()
    heartbeat_age = None
    if last_snapshot is not None:
        stamped = last_snapshot if last_snapshot.tzinfo else last_snapshot.replace(tzinfo=now.tzinfo)
        heartbeat_age = int((now - stamped).total_seconds() // 60)

    blocks: list[dict] = []
    if engaged:
        blocks.append(
            {
                "control": "Kill switch",
                "detail": ks_reason or "Engaged by an operator.",
                "severity": "halted",
            }
        )
    if tripped:
        blocks.append(
            {
                "control": "Circuit breaker",
                "detail": cb_reason or "Tripped after repeated broker errors.",
                "severity": "halted",
            }
        )
    # A stalled worker is NOT a halt: nothing is blocked, nothing is running.
    # Conflating them would send the operator to the wrong fix.
    if heartbeat_age is not None and heartbeat_age > HEARTBEAT_STALE_MINUTES:
        blocks.append(
            {
                "control": "Worker",
                "detail": f"No activity for {heartbeat_age} minutes. The worker may not be running.",
                "severity": "stalled",
            }
        )

    return {
        "environment": settings.trading_env,
        "healthy": not blocks,
        "blocks": blocks,
        "live": {
            "gates_satisfied": settings.live_trading_allowed(),
            "armed": armed,
            "effective": settings.live_trading_allowed() and armed,
        },
        "heartbeat_minutes": heartbeat_age,
    }


async def _portfolio(
    session: AsyncSession, broker: Broker | None, range_key: str, now: datetime
) -> dict:
    window = RANGES.get(range_key, RANGES[DEFAULT_RANGE])
    stmt = sa.select(PositionSnapshot).order_by(PositionSnapshot.created_at.asc())
    if window is not None:
        stmt = stmt.where(PositionSnapshot.created_at >= now - window)
    snapshots = (await session.execute(stmt)).scalars().all()

    series = [(s.created_at, s.equity) for s in snapshots]
    chart = build_line_chart(series)

    live_equity = live_cash = None
    broker_error = None
    if broker is not None:
        try:
            account = await broker.get_account()
            live_equity, live_cash = float(account.equity), float(account.cash)
        except BrokerError as exc:
            broker_error = str(exc)
            log.warning("dashboard_account_unavailable", error=str(exc))

    # Prefer the broker's live figure; fall back to the newest snapshot.
    equity = live_equity
    if equity is None and snapshots:
        equity = float(snapshots[-1].equity)

    return {
        "range": range_key,
        "ranges": list(RANGES),
        "equity": equity,
        "cash": live_cash,
        "broker_error": broker_error,
        "chart": None
        if chart is None
        else {
            "points": [{"ts": p.ts, "value": p.value, "x": p.x, "y": p.y} for p in chart.points],
            "direction": chart.direction,
            "flat": chart.flat,
            "change_abs": chart.change_abs,
            "change_pct": chart.change_pct,
            "first": chart.first,
            "last": chart.last,
            "y_min": chart.y_min,
            "y_max": chart.y_max,
        },
        "snapshot_count": len(snapshots),
    }


async def _holdings(broker: Broker | None, cash: float | None) -> dict:
    if broker is None:
        return {"available": False, "error": "No broker configured.", "rows": [], "allocation": []}
    try:
        positions = await broker.get_positions()
    except BrokerError as exc:
        return {"available": False, "error": str(exc), "rows": [], "allocation": []}

    rows = [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "value": float(p.market_value),
            "avg_entry": float(p.avg_entry_price),
            "unrealized": float(p.unrealized_pl),
            "unrealized_pct": (
                float(p.unrealized_pl) / (float(p.avg_entry_price) * float(p.qty))
                if p.avg_entry_price and p.qty
                else 0.0
            ),
        }
        for p in positions
    ]
    rows.sort(key=lambda r: r["value"], reverse=True)
    allocation = build_allocation(
        [(r["symbol"], r["value"]) for r in rows], cash if cash is not None else 0.0
    )
    return {
        "available": True,
        "error": None,
        "rows": rows,
        "allocation": [
            {"label": a.label, "value": a.value, "fraction": a.fraction, "is_cash": a.is_cash}
            for a in allocation
        ],
    }


def _decision_reasoning(decision: Decision) -> dict:
    """Translate one decision's stored context and risk detail into sentences."""
    context = decision.context or {}
    risk_detail = decision.risk_detail or {}
    results = risk_detail.get("results") or []

    rejections = [
        copy.rejection_sentence(r.get("rule", ""), r)
        for r in results
        if r.get("verdict") and r["verdict"] != RiskVerdict.APPROVED
    ]
    if not rejections and risk_detail.get("error"):
        rejections = [f"Could not evaluate risk: {risk_detail.get('detail', 'unknown error')}"]

    criteria = [
        {"label": copy.criterion_label(key), "passed": bool(value)}
        for key, value in sorted(context.items())
        if key.startswith("c") and isinstance(value, bool)
    ]

    facts = []
    for key, value in context.items():
        if key in ("features", "regime", "signal_source") or key.startswith("c"):
            continue
        if isinstance(value, (dict, list)):
            continue
        display = value
        if key == "exit_reason":
            display = copy.exit_reason_label(str(value))
        facts.append({"label": copy.context_label(key), "value": str(display)})
    facts.sort(key=lambda f: f["label"])

    regime = context.get("regime")
    return {
        "rejections": rejections,
        "criteria": criteria,
        "facts": facts,
        "regime": copy.REGIME_LABEL.get(regime, copy.humanize(regime)) if regime else None,
        "rs_skipped": str(context.get("rs_skipped", "")).lower() == "true",
    }


async def _activity(session: AsyncSession, now: datetime, limit: int = 60) -> dict:
    decisions = (
        await session.execute(
            sa.select(Decision).order_by(Decision.created_at.desc()).limit(limit)
        )
    ).scalars().all()

    strategy_names = {
        row.id: row.name
        for row in (await session.execute(sa.select(Strategy))).scalars().all()
    }

    rows = []
    for d in decisions:
        label, tone = copy.status_label(d.status, copy.DECISION_STATUS)
        rows.append(
            {
                "id": str(d.id),
                "ts": d.created_at.isoformat(),
                "symbol": d.symbol,
                "action": d.action,
                "qty": float(d.qty),
                "mode": d.mode,
                "mode_label": copy.MODE_LABEL.get(d.mode, copy.humanize(d.mode)),
                "status": d.status,
                "status_label": label,
                "tone": tone,
                "strategy": strategy_names.get(d.strategy_id, "—"),
                "reasoning": _decision_reasoning(d),
            }
        )

    # 14-day executed-vs-rejected volume.
    since = (now - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)
    counts = (
        await session.execute(
            sa.select(
                sa.func.date(Decision.created_at), Decision.status, sa.func.count(Decision.id)
            )
            .where(Decision.created_at >= since)
            .group_by(sa.func.date(Decision.created_at), Decision.status)
        )
    ).all()
    per_day: dict[str, dict[str, int]] = {}
    for day, status, count in counts:
        key = str(day)
        bucket = per_day.setdefault(key, {"executed": 0, "rejected": 0})
        if status in (DecisionStatus.EXECUTED, DecisionStatus.APPROVED, DecisionStatus.CLOSED):
            bucket["executed"] += count
        elif status in (DecisionStatus.REJECTED, DecisionStatus.FAILED):
            bucket["rejected"] += count
    buckets = []
    for offset in range(14):
        day = (since + timedelta(days=offset)).date().isoformat()
        entry = per_day.get(day, {"executed": 0, "rejected": 0})
        buckets.append((day, entry["executed"], entry["rejected"]))

    return {
        "rows": rows,
        "volume": [
            {
                "label": v.label,
                "executed": v.executed,
                "rejected": v.rejected,
                "total": v.total,
                "fraction": v.executed_fraction,
            }
            for v in build_volume(buckets)
        ],
    }


async def _personas(session: AsyncSession) -> list[dict]:
    personas = (
        await session.execute(sa.select(Persona).order_by(Persona.name))
    ).scalars().all()
    bindings = {b.name: b for b in await load_bindings(session)}
    return [
        {
            # Needed by the dashboard's Start/Stop controls, which call the
            # existing audited /admin/personas endpoints by id.
            "id": str(p.id),
            "name": p.name,
            "kind": p.kind,
            "cooperative": p.kind == PersonaKind.COOPERATIVE,
            "active": p.status == PersonaStatus.ACTIVE,
            "status": p.status,
            "members": len(bindings[p.name].strategy_ids) if p.name in bindings else 0,
            "fidelity_note": p.fidelity_note,
        }
        for p in personas
    ]


async def _rejections(session: AsyncSession, now: datetime) -> list[dict]:
    rows = (
        await session.execute(
            sa.select(RiskEvent.rule, sa.func.count(RiskEvent.id))
            .where(
                RiskEvent.created_at >= now - timedelta(days=1),
                RiskEvent.verdict != RiskVerdict.APPROVED,
            )
            .group_by(RiskEvent.rule)
            .order_by(sa.func.count(RiskEvent.id).desc())
        )
    ).all()
    return [
        {
            "rule": rule,
            "label": copy.rule_label(rule),
            "explanation": copy.rule_explanation(rule),
            "count": count,
        }
        for rule, count in rows
    ]


async def _counts(session: AsyncSession) -> dict:
    tradeable = (
        await session.execute(
            sa.select(sa.func.count(Strategy.id)).where(
                Strategy.status.in_([StrategyStatus.APPROVED, StrategyStatus.CHAMPION])
            )
        )
    ).scalar_one()
    open_orders = (
        await session.execute(
            sa.select(sa.func.count(Order.id)).where(
                Order.status.notin_(list(OrderStatus.terminal()))
            )
        )
    ).scalar_one()
    return {"tradeable_strategies": int(tradeable), "open_orders": int(open_orders)}


async def build_dashboard(
    session: AsyncSession,
    settings: Settings,
    broker: Broker | None,
    range_key: str = DEFAULT_RANGE,
) -> dict[str, Any]:
    """Everything the dashboard needs, in one JSON-ready payload."""
    now = utcnow()
    if range_key not in RANGES:
        range_key = DEFAULT_RANGE

    system = await _system(session, settings, now)
    portfolio = await _portfolio(session, broker, range_key, now)
    holdings = await _holdings(broker, portfolio.get("cash"))
    activity = await _activity(session, now)
    personas = await _personas(session)
    rejections = await _rejections(session, now)
    counts = await _counts(session)

    return {
        "generated_at": now.isoformat(),
        "system": system,
        "portfolio": portfolio,
        "holdings": holdings,
        "activity": activity,
        "personas": personas,
        "rejections": rejections,
        "counts": counts,
        "hints": _hints(system, personas, counts, activity),
    }


def _hints(system: dict, personas: list[dict], counts: dict, activity: dict) -> list[str]:
    """Plain-language explanations for states that are correct but look broken.

    This is the difference between a dashboard that confuses and one that
    teaches: an empty feed because no persona is active is not an error, and
    the surface should say so rather than leaving the operator guessing.
    """
    hints: list[str] = []
    if not any(p["active"] for p in personas):
        hints.append("No personas are active, so nothing will trade.")
    if counts["tradeable_strategies"] == 0:
        hints.append("No strategies are approved yet, so the strategy loop has nothing to run.")
    if not activity["rows"] and system["healthy"]:
        hints.append("No decisions recorded yet. The strategy loop runs every 15 minutes.")
    return hints
