"""Operator/admin API — the only place runtime controls and promotions change.

Every mutating endpoint requires the admin token, takes an explicit human
``actor`` and ``reason``, and writes an audit row (RiskEvent or
PromotionEvent). The learning system has no access to these endpoints; they
exist precisely so promotion and arming stay human actions.
"""

from __future__ import annotations

import uuid
from decimal import ROUND_DOWN, Decimal
from typing import Annotated, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_execution_service, require_admin
from app.brokers.base import BrokerError
from app.config import Settings, get_settings
from app.db.base import get_db_session, utcnow
from app.db.models import (
    Decision,
    DecisionMode,
    Environment,
    MarketBar,
    ModelStatus,
    Order,
    OrderStatus,
    PromotionEvent,
    RiskEvent,
    RiskVerdict,
    SignalAction,
    Strategy,
    StrategyStatus,
)
from app.learning.registry import ModelRegistry
from app.logging import get_logger
from app.profile.model import load_profile
from app.risk import state as controls
from app.schemas.core import TradeIntent

log = get_logger("api.admin")

router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)]
)

DbDep = Annotated[AsyncSession, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


class ActorRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=512)


# ---------------------------------------------------------------- status


@router.get("/status")
async def status(session: DbDep, settings: SettingsDep) -> dict:
    engaged, ks_reason = await controls.kill_switch_engaged(session)
    tripped, cb_reason = await controls.circuit_breaker_tripped(session, utcnow())
    armed = await controls.live_trading_armed(session)
    open_orders = (
        await session.execute(
            sa.select(sa.func.count(Order.id)).where(
                Order.status.notin_(["filled", "canceled", "rejected", "expired", "error"])
            )
        )
    ).scalar_one()
    return {
        "trading_env": settings.trading_env,
        "kill_switch": {"engaged": engaged, "reason": ks_reason},
        "circuit_breaker": {"tripped": tripped, "reason": cb_reason},
        "live_trading": {
            "config_gates_satisfied": settings.live_trading_allowed(),
            "runtime_armed": armed,
            "effective": settings.live_trading_allowed() and armed,
        },
        "open_orders": open_orders,
    }


# ---------------------------------------------------------------- kill switch


class KillSwitchRequest(ActorRequest):
    engage: bool


@router.post("/kill-switch")
async def kill_switch(req: KillSwitchRequest, session: DbDep, settings: SettingsDep) -> dict:
    env = Environment(settings.trading_env)
    if req.engage:
        await controls.engage_kill_switch(session, req.actor, req.reason, env)
    else:
        await controls.release_kill_switch(session, req.actor, req.reason, env)
    await session.commit()
    return {"kill_switch_engaged": req.engage}


@router.post("/circuit-breaker/reset")
async def reset_breaker(req: ActorRequest, session: DbDep, settings: SettingsDep) -> dict:
    await controls.reset_circuit_breaker(
        session, req.actor, req.reason, Environment(settings.trading_env)
    )
    await session.commit()
    return {"circuit_breaker_tripped": False}


# ---------------------------------------------------------------- live arming


class ArmRequest(ActorRequest):
    armed: bool


@router.post("/live/arm")
async def arm_live(req: ArmRequest, session: DbDep, settings: SettingsDep) -> dict:
    if req.armed and not settings.live_trading_allowed():
        raise HTTPException(
            status_code=400,
            detail="config gates not satisfied; arming would have no effect and is refused",
        )
    await controls.set_live_armed(session, req.armed, req.actor, req.reason)
    await session.commit()
    return {"live_trading_armed": req.armed}


# ---------------------------------------------------------------- strategy lifecycle


_STRATEGY_TRANSITIONS: dict[str, set[str]] = {
    StrategyStatus.CANDIDATE: {StrategyStatus.APPROVED, StrategyStatus.RETIRED},
    StrategyStatus.APPROVED: {StrategyStatus.CHAMPION, StrategyStatus.RETIRED},
    StrategyStatus.CHAMPION: {StrategyStatus.RETIRED},
    StrategyStatus.RETIRED: set(),
}


class StrategyStatusRequest(ActorRequest):
    to_status: Literal["approved", "champion", "retired"]


@router.post("/strategies/{strategy_id}/status")
async def set_strategy_status(
    strategy_id: uuid.UUID, req: StrategyStatusRequest, session: DbDep
) -> dict:
    row = await session.get(Strategy, strategy_id)
    if row is None:
        raise HTTPException(status_code=404, detail="strategy not found")
    if req.to_status not in _STRATEGY_TRANSITIONS.get(row.status, set()):
        raise HTTPException(
            status_code=400,
            detail=f"illegal transition {row.status} -> {req.to_status}",
        )
    from_status = row.status
    if req.to_status == StrategyStatus.CHAMPION:
        # Single champion: demote any current champion to approved.
        current = (
            await session.execute(
                sa.select(Strategy).where(Strategy.status == StrategyStatus.CHAMPION)
            )
        ).scalars().all()
        for champ in current:
            champ.status = StrategyStatus.APPROVED
            session.add(
                PromotionEvent(
                    subject_type="strategy",
                    subject_id=champ.id,
                    from_status=StrategyStatus.CHAMPION,
                    to_status=StrategyStatus.APPROVED,
                    actor=req.actor,
                    reason=f"displaced by {row.name} v{row.version}",
                )
            )
    row.status = req.to_status
    if req.to_status == StrategyStatus.APPROVED:
        row.approved_by = req.actor
        row.approved_at = utcnow()
    session.add(
        PromotionEvent(
            subject_type="strategy",
            subject_id=row.id,
            from_status=from_status,
            to_status=req.to_status,
            actor=req.actor,
            reason=req.reason,
        )
    )
    await session.commit()
    return {"strategy": row.name, "version": row.version, "status": row.status}


# ---------------------------------------------------------------- model lifecycle


class ModelPromoteRequest(ActorRequest):
    to_status: Literal["shadow", "champion", "retired"]


@router.post("/models/{model_id}/promote")
async def promote_model(model_id: uuid.UUID, req: ModelPromoteRequest, session: DbDep) -> dict:
    registry = ModelRegistry()
    try:
        row = await registry.promote(
            session, model_id, ModelStatus(req.to_status), actor=req.actor, reason=req.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await session.commit()
    return {"model": row.name, "version": row.version, "status": row.status}


# ---------------------------------------------------------------- manual orders


class ManualOrderRequest(ActorRequest):
    symbol: str = Field(min_length=1, max_length=16)
    action: Literal["buy", "sell", "close"]
    #: Omit to size from the owner profile's per-position target.
    qty: Decimal | None = Field(default=None, gt=0)


@router.post("/orders")
async def place_manual_order(
    req: ManualOrderRequest,
    session: DbDep,
    settings: SettingsDep,
    execution: Annotated[object | None, Depends(get_execution_service)],
) -> dict:
    """Place a one-off human-initiated order.

    **This is not a bypass.** It builds an ordinary ``TradeIntent`` and hands
    it to the same ``ExecutionService`` the strategy loop uses, so it creates a
    Decision row and passes the full risk engine: kill switch, every limit,
    duplicate detection. A manual order that violates a limit is rejected like
    any other, and the rejection is recorded with its reasoning.

    The decision carries ``strategy_id: null`` and an ``origin: manual``
    context, which is what keeps a human's discretionary call out of the
    allocator's training data.
    """
    if execution is None:
        raise HTTPException(
            status_code=503,
            detail="no broker configured; set Alpaca credentials before trading",
        )

    symbol = req.symbol.strip().upper()
    qty = req.qty
    sized_from_profile = False

    if qty is None:
        # Size from the profile, exactly as the strategy path does.
        price = (
            await session.execute(
                sa.select(MarketBar.close)
                .where(MarketBar.symbol == symbol)
                .order_by(MarketBar.ts.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if price is None or price <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"no price on record for {symbol}; pass an explicit qty",
            )
        profile = load_profile()
        qty = (profile.per_position_target() / price).quantize(
            Decimal("1"), rounding=ROUND_DOWN
        )
        sized_from_profile = True
        if qty <= 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"one share of {symbol} at {price} exceeds the profile's "
                    f"per-position budget {profile.per_position_target()}"
                ),
            )

    context = {
        "origin": "manual",
        "actor": req.actor,
        "manual_reason": req.reason,
    }
    if sized_from_profile:
        context["sized_by"] = "profile"

    intent = TradeIntent(
        symbol=symbol,
        action=SignalAction(req.action),
        qty=qty,
        mode=DecisionMode.PAPER,
        context=context,
    )

    try:
        decision = await execution.execute_intent(session, intent)
        await session.commit()
    finally:
        aclose = getattr(execution.broker, "aclose", None)
        if aclose is not None:
            await aclose()

    log.info(
        "manual_order",
        symbol=symbol,
        action=req.action,
        qty=str(qty),
        actor=req.actor,
        status=decision.status,
    )
    rejections = [
        r["reason"]
        for r in (decision.risk_detail or {}).get("results", [])
        if r.get("verdict") and r["verdict"] != RiskVerdict.APPROVED
    ]
    return {
        "decision_id": str(decision.id),
        "symbol": symbol,
        "action": req.action,
        "qty": str(qty),
        "sized_by": "profile" if sized_from_profile else "operator",
        "status": decision.status,
        "risk_verdict": decision.risk_verdict,
        "rejections": rejections,
    }


@router.post("/orders/{client_order_id}/cancel")
async def cancel_order(
    client_order_id: str,
    req: ActorRequest,
    session: DbDep,
    settings: SettingsDep,
    execution: Annotated[object | None, Depends(get_execution_service)],
) -> dict:
    """Withdraw a working order.

    Without this the platform can place orders it has no way to recall, which
    is what turned an ordinary sizing bug into an account that could not be
    unwound. Cancelling is always allowed: it can only reduce exposure.
    """
    if execution is None:
        raise HTTPException(status_code=503, detail="no broker configured")

    order = (
        await session.execute(
            sa.select(Order).where(Order.client_order_id == client_order_id)
        )
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    if order.status in OrderStatus.terminal():
        return {"client_order_id": client_order_id, "status": order.status,
                "note": "already in a terminal state; nothing to cancel"}

    try:
        await execution.broker.cancel_order(client_order_id)
    except BrokerError as exc:
        raise HTTPException(status_code=502, detail=f"broker refused cancel: {exc}")
    finally:
        aclose = getattr(execution.broker, "aclose", None)
        if aclose is not None:
            await aclose()

    order.status = OrderStatus.CANCELED
    order.status_reason = f"cancelled by {req.actor}: {req.reason}"[:500]
    order.closed_at = utcnow()
    session.add(
        RiskEvent(
            rule="manual_cancel",
            verdict=RiskVerdict.APPROVED,
            severity="warning",
            environment=Environment(settings.trading_env),
            detail={
                "client_order_id": client_order_id,
                "symbol": order.symbol,
                "actor": req.actor,
                "reason": req.reason,
            },
        )
    )
    await session.commit()
    log.info("manual_cancel", client_order_id=client_order_id, actor=req.actor)
    return {"client_order_id": client_order_id, "status": order.status}


# ---------------------------------------------------------------- personas


@router.get("/personas")
async def list_personas(session: DbDep) -> list[dict]:
    from app.personas.service import load_personas, member_strategies

    out = []
    for persona in await load_personas(session):
        members = await member_strategies(session, persona)
        out.append(
            {
                "id": str(persona.id),
                "name": persona.name,
                "kind": persona.kind,
                "status": persona.status,
                "description": persona.description,
                "fidelity_note": persona.fidelity_note,
                "symbol_set": persona.symbol_set,
                "activated_by": persona.activated_by,
                "members": [
                    {
                        "id": str(m.id),
                        "name": m.name,
                        "version": m.version,
                        "status": m.status,
                        "params": m.params,
                    }
                    for m in members
                ],
            }
        )
    return out


@router.post("/personas/{persona_id}/activate")
async def activate_persona(
    persona_id: uuid.UUID, req: ActorRequest, session: DbDep
) -> dict:
    """Activate a persona, approving candidate members through the same
    audited transition path a manual approval uses. Never a bypass."""
    from app.personas.service import activate, get_persona

    persona = await get_persona(session, persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail="persona not found")
    try:
        result = await activate(session, persona, actor=req.actor, reason=req.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await session.commit()
    return {"persona": persona.name, "status": persona.status, **result}


@router.post("/personas/{persona_id}/deactivate")
async def deactivate_persona(
    persona_id: uuid.UUID, req: ActorRequest, session: DbDep
) -> dict:
    """Stop a persona trading. Members stay approved but become shadow-only,
    so this is reversible — no terminal status change required."""
    from app.personas.service import deactivate, get_persona

    persona = await get_persona(session, persona_id)
    if persona is None:
        raise HTTPException(status_code=404, detail="persona not found")
    try:
        await deactivate(session, persona, actor=req.actor, reason=req.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    await session.commit()
    return {"persona": persona.name, "status": persona.status}


# ---------------------------------------------------------------- inspection


@router.get("/risk-events")
async def recent_risk_events(session: DbDep, limit: int = 50) -> list[dict]:
    rows = (
        await session.execute(
            sa.select(RiskEvent).order_by(RiskEvent.created_at.desc()).limit(min(limit, 500))
        )
    ).scalars().all()
    return [
        {
            "rule": r.rule,
            "verdict": r.verdict,
            "severity": r.severity,
            "environment": r.environment,
            "detail": r.detail,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/decisions")
async def recent_decisions(session: DbDep, limit: int = 50) -> list[dict]:
    rows = (
        await session.execute(
            sa.select(Decision).order_by(Decision.created_at.desc()).limit(min(limit, 500))
        )
    ).scalars().all()
    return [
        {
            "id": str(d.id),
            "mode": d.mode,
            "symbol": d.symbol,
            "action": d.action,
            "qty": str(d.qty),
            "status": d.status,
            "risk_verdict": d.risk_verdict,
            "created_at": d.created_at.isoformat(),
        }
        for d in rows
    ]
