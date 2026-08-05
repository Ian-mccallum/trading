"""Operator-controlled runtime switches, DB-backed so every process (API,
workers) sees the same state immediately.

- Kill switch: engaged → all order flow halts, everywhere.
- Live arming: even with every config gate satisfied, live orders require this
  runtime switch — an operator action recorded with actor + reason.
- Circuit breaker: tripped automatically by the risk engine on repeated broker
  errors; clears after its expiry or by operator reset.

All changes are audited as RiskEvents.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import (
    CONTROL_CIRCUIT_BREAKER,
    CONTROL_KILL_SWITCH,
    CONTROL_LIVE_ARMED,
    Environment,
    RiskEvent,
    RiskVerdict,
    SystemControl,
)


async def get_control(session: AsyncSession, key: str) -> dict[str, Any]:
    row = await session.get(SystemControl, key)
    return dict(row.value) if row else {}


async def set_control(
    session: AsyncSession, key: str, value: dict[str, Any], actor: str
) -> None:
    row = await session.get(SystemControl, key)
    if row is None:
        row = SystemControl(key=key, value=value, updated_by=actor)
        session.add(row)
    else:
        row.value = value
        row.updated_by = actor
    await session.flush()


async def _audit(
    session: AsyncSession,
    rule: str,
    verdict: RiskVerdict,
    environment: Environment,
    detail: dict[str, Any],
    severity: str = "warning",
) -> None:
    session.add(
        RiskEvent(
            rule=rule,
            verdict=verdict,
            severity=severity,
            environment=environment,
            detail=detail,
        )
    )
    await session.flush()


# ---------------------------------------------------------------- kill switch


async def kill_switch_engaged(session: AsyncSession) -> tuple[bool, str]:
    state = await get_control(session, CONTROL_KILL_SWITCH)
    return bool(state.get("engaged", False)), str(state.get("reason", ""))


async def engage_kill_switch(
    session: AsyncSession, actor: str, reason: str, environment: Environment
) -> None:
    await set_control(
        session, CONTROL_KILL_SWITCH, {"engaged": True, "reason": reason}, actor
    )
    await _audit(
        session,
        "kill_switch",
        RiskVerdict.HALTED,
        environment,
        {"actor": actor, "reason": reason, "engaged": True},
        severity="critical",
    )


async def release_kill_switch(
    session: AsyncSession, actor: str, reason: str, environment: Environment
) -> None:
    await set_control(
        session, CONTROL_KILL_SWITCH, {"engaged": False, "reason": reason}, actor
    )
    await _audit(
        session,
        "kill_switch",
        RiskVerdict.APPROVED,
        environment,
        {"actor": actor, "reason": reason, "engaged": False},
        severity="warning",
    )


# ---------------------------------------------------------------- live arming


async def live_trading_armed(session: AsyncSession) -> bool:
    state = await get_control(session, CONTROL_LIVE_ARMED)
    return bool(state.get("armed", False))


async def set_live_armed(
    session: AsyncSession, armed: bool, actor: str, reason: str
) -> None:
    await set_control(session, CONTROL_LIVE_ARMED, {"armed": armed}, actor)
    await _audit(
        session,
        "live_trading_armed",
        RiskVerdict.APPROVED if not armed else RiskVerdict.HALTED,
        Environment.LIVE,
        {"actor": actor, "reason": reason, "armed": armed},
        severity="critical",
    )


# ---------------------------------------------------------------- circuit breaker


async def circuit_breaker_state(session: AsyncSession) -> dict[str, Any]:
    return await get_control(session, CONTROL_CIRCUIT_BREAKER)


async def circuit_breaker_tripped(session: AsyncSession, now: datetime) -> tuple[bool, str]:
    state = await circuit_breaker_state(session)
    if not state.get("tripped", False):
        return False, ""
    until_raw = state.get("until")
    if until_raw:
        until = datetime.fromisoformat(until_raw)
        if now >= until:
            return False, ""  # expired; treated as clear (reset lazily by engine)
    return True, str(state.get("reason", ""))


async def trip_circuit_breaker(
    session: AsyncSession,
    reason: str,
    environment: Environment,
    cooldown_seconds: int,
    actor: str = "risk_engine",
) -> None:
    until = (utcnow() + timedelta(seconds=cooldown_seconds)).isoformat()
    await set_control(
        session,
        CONTROL_CIRCUIT_BREAKER,
        {"tripped": True, "reason": reason, "until": until},
        actor,
    )
    await _audit(
        session,
        "circuit_breaker",
        RiskVerdict.HALTED,
        environment,
        {"reason": reason, "until": until, "actor": actor},
        severity="critical",
    )


async def reset_circuit_breaker(
    session: AsyncSession, actor: str, reason: str, environment: Environment
) -> None:
    await set_control(
        session, CONTROL_CIRCUIT_BREAKER, {"tripped": False, "reason": reason}, actor
    )
    await _audit(
        session,
        "circuit_breaker",
        RiskVerdict.APPROVED,
        environment,
        {"actor": actor, "reason": reason, "tripped": False},
        severity="warning",
    )


async def count_recent_broker_errors(
    session: AsyncSession, environment: Environment, window_seconds: int, now: datetime
) -> int:
    cutoff = now - timedelta(seconds=window_seconds)
    result = await session.execute(
        sa.select(sa.func.count(RiskEvent.id)).where(
            RiskEvent.rule == "broker_error",
            RiskEvent.environment == environment,
            RiskEvent.created_at >= cutoff,
        )
    )
    return int(result.scalar_one())


async def record_broker_error(
    session: AsyncSession, environment: Environment, detail: dict[str, Any]
) -> None:
    await _audit(
        session,
        "broker_error",
        RiskVerdict.REJECTED,
        environment,
        detail,
        severity="error",
    )
