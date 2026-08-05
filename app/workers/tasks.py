"""arq background tasks — the trading loop.

Everything that trades runs here, not in the API process:

- ``process_signal``: turn a validated TradingView signal into a TradeIntent
  and hand it to the execution service (which runs the risk engine).
- ``run_strategies``: the periodic strategy loop. The champion allocator model
  picks ONE strategy per symbol to trade (paper); every other approved
  strategy runs in SHADOW mode so the learner gathers counterfactual data.
- ``sync_orders`` / ``snapshot_positions`` / ``reconcile_orders``: lifecycle.
- ``evaluate_outcomes``: closes decisions with realized/hypothetical outcomes.
- ``train_allocator_job``: offline retraining. Registers a new INERT model
  version; promotion to shadow/champion is a human admin action.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import (
    DecisionMode,
    DecisionStatus,
    MarketBar,
    Signal,
    SignalAction,
    SignalStatus,
    StrategyStatus,
)
from app.db.models import (
    Strategy as StrategyRow,
)
from app.execution.service import size_qty
from app.logging import get_logger
from app.schemas.core import BarData, TradeIntent

log = get_logger("workers.tasks")

#: Bars loaded per symbol for the strategy loop. Must exceed the largest
#: ``warmup_bars()`` in the catalog or those strategies are silently skipped —
#: Minervini needs 253 (200-day SMA + 21-day rising check, and a 252-bar
#: 52-week window) and PTJ needs 201. 400 leaves headroom for longer-lookback
#: strategies without loading years of history every cycle.
BAR_LOOKBACK = 400
STRATEGY_TIMEFRAME = "1Day"


def _broker_configured(ctx: dict) -> bool:
    """Broker-facing cron tasks no-op (with a log line) until credentials for
    the active environment are configured, instead of stack-tracing on 401s."""
    settings = ctx["settings"]
    if settings.trading_env == "live":
        configured = bool(settings.alpaca_live_api_key)
    else:
        configured = bool(settings.alpaca_paper_api_key)
    if not configured:
        log.info("broker_task_skipped_no_credentials", trading_env=settings.trading_env)
    return configured


async def _recent_bars(
    session: AsyncSession, symbol: str, timeframe: str, limit: int = BAR_LOOKBACK
) -> list[BarData]:
    rows = (
        (
            await session.execute(
                sa.select(MarketBar)
                .where(MarketBar.symbol == symbol, MarketBar.timeframe == timeframe)
                .order_by(MarketBar.ts.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        BarData(
            symbol=r.symbol, timeframe=r.timeframe, ts=r.ts,
            open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume,
        )
        for r in reversed(rows)
    ]


# ---------------------------------------------------------------- signals


async def process_signal(ctx: dict, signal_id: str) -> str:
    from app.learning.features import market_context

    settings = ctx["settings"]
    execution = ctx["execution"]
    async with ctx["session_factory"]() as session:
        signal = await session.get(Signal, uuid.UUID(signal_id))
        if signal is None:
            return "missing"
        if signal.status != SignalStatus.VALIDATED:
            return f"skipped:{signal.status}"

        payload = signal.payload or {}
        bars = await _recent_bars(session, signal.symbol, STRATEGY_TIMEFRAME)
        context = market_context(bars) if bars else {"features": {}, "regime": "unknown"}
        context["signal_source"] = signal.source

        strategy_id = None
        if payload.get("strategy"):
            row = (
                await session.execute(
                    sa.select(StrategyRow)
                    .where(
                        StrategyRow.name == payload["strategy"],
                        StrategyRow.status.in_(
                            [StrategyStatus.APPROVED, StrategyStatus.CHAMPION]
                        ),
                    )
                    .order_by(StrategyRow.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            strategy_id = row.id if row else None

        qty = Decimal(str(payload["qty"])) if payload.get("qty") else None
        if qty is None:
            last_close = bars[-1].close if bars else None
            if last_close is None:
                signal.status = SignalStatus.REJECTED
                signal.status_reason = "no market data to size order"
                await session.commit()
                return "rejected:no_data"
            account = await execution.broker.get_account()
            qty = size_qty(
                account.equity, last_close, Decimal(str(settings.risk_max_order_notional))
            )
        if qty <= 0:
            signal.status = SignalStatus.REJECTED
            signal.status_reason = "sized to zero"
            await session.commit()
            return "rejected:zero_qty"

        intent = TradeIntent(
            symbol=signal.symbol,
            action=SignalAction(signal.action),
            qty=qty,
            mode=DecisionMode.PAPER,
            strategy_id=strategy_id,
            signal_id=signal.id,
            context=context,
        )
        try:
            decision = await execution.execute_intent(session, intent)
        except Exception as exc:  # broker/config failures must not lose the signal
            log.error("process_signal_failed", signal_id=signal_id, error=str(exc))
            signal.status = SignalStatus.REJECTED
            signal.status_reason = f"execution error: {exc}"[:250]
            await session.commit()
            return "error"
        signal.status = (
            SignalStatus.PROCESSED
            if decision.status in (DecisionStatus.EXECUTED, DecisionStatus.APPROVED)
            else SignalStatus.REJECTED
        )
        signal.status_reason = f"decision:{decision.status}"
        signal.processed_at = utcnow()
        await session.commit()
        return str(decision.status)


# ---------------------------------------------------------------- strategy loop


async def run_strategies(ctx: dict) -> dict:
    if not _broker_configured(ctx):
        return {'note': 'skipped: no broker credentials'}
    from app.learning.allocator import load_champion_allocator
    from app.learning.features import market_context
    from app.personas.orchestration import assign_modes, competing_pool
    from app.personas.service import load_bindings, load_personas
    from app.profile.model import load_profile
    from app.profile.sizing import cap_open_positions, size_intent
    from app.strategies import registry
    from app.strategies.base import StrategyContext
    from app.strategies.benchmark import (
        FEATURE_KEY,
        PEERS_KEY,
        build_benchmark_features,
        build_peer_features,
    )

    settings = ctx["settings"]
    execution = ctx["execution"]
    registry.load_builtins()
    summary: dict[str, str] = {}

    async with ctx["session_factory"]() as session:
        rows = (
            (
                await session.execute(
                    sa.select(StrategyRow).where(
                        StrategyRow.status.in_(
                            [StrategyStatus.APPROVED, StrategyStatus.CHAMPION]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return {"note": "no approved strategies"}
        allocator = await load_champion_allocator(session)
        # Persona bindings decide which strategies cooperate (all trade) and
        # which compete (allocator picks one) — see app/personas/orchestration.
        bindings = await load_bindings(session)
        pool = competing_pool(rows, bindings)

        # Owner profile drives sizing and symbol preferences. A missing file is
        # normal and yields conservative defaults.
        profile = load_profile()
        # Map each strategy to its persona so weights apply. A strategy in more
        # than one persona takes the first by name for determinism.
        persona_of: dict = {}
        for persona in await load_personas(session):
            for member in persona.members:
                persona_of.setdefault(member.strategy_id, persona.name)

        account = await execution.broker.get_account()
        positions = {p.symbol: p for p in await execution.broker.get_positions()}

        # Benchmark summary for relative-strength criteria, loaded once and
        # shared. Absent/short history simply means the key is missing and
        # consumers skip those criteria.
        benchmark_features: dict = {}
        if settings.strategy_benchmark_symbol:
            bench_symbol = settings.strategy_benchmark_symbol.upper()
            bench_bars = await _recent_bars(session, bench_symbol, STRATEGY_TIMEFRAME)
            summary_dict = build_benchmark_features(bench_symbol, bench_bars)
            if summary_dict:
                benchmark_features[FEATURE_KEY] = summary_dict
            else:
                log.info("benchmark_unavailable", symbol=bench_symbol, bars=len(bench_bars))

        # Load every allowlisted symbol's bars once: the strategy loop needs
        # them anyway, and summarizing them into peer features is what makes
        # cross-symbol comparisons (dual momentum's relative leg) possible
        # without breaking the single-symbol strategy contract.
        symbols = sorted(settings.symbol_allowlist() or {"SPY"})
        bars_by_symbol = {
            symbol: await _recent_bars(session, symbol, STRATEGY_TIMEFRAME)
            for symbol in symbols
        }
        peer_features = build_peer_features(
            {s: b for s, b in bars_by_symbol.items() if b}
        )
        shared_features = {**benchmark_features}
        if peer_features:
            shared_features[PEERS_KEY] = peer_features

        for symbol in symbols:
            bars = bars_by_symbol.get(symbol) or []
            if not bars:
                summary[symbol] = "no_bars"
                continue
            mkt = market_context(bars)
            mkt["features"] = {**mkt["features"], **shared_features}
            selected = allocator.select(mkt["regime"], pool)
            modes = assign_modes(
                [r.id for r in rows], bindings, selected.id if selected else None
            )
            for row in rows:
                strategy = registry.instantiate(row)
                if len(bars) < strategy.warmup_bars():
                    continue
                sctx = StrategyContext(
                    symbol=symbol,
                    bars=bars,
                    position=positions.get(symbol),
                    equity=account.equity,
                    # Portfolio weights are fractions of what the profile
                    # allocates, not of the whole account.
                    investable=min(profile.capital_allocation, account.equity),
                    features=mkt["features"],
                )
                intents = strategy.on_bars(sctx)
                mode = modes.get(row.id, DecisionMode.SHADOW)

                # Profile sizing: strategies express direction, the profile
                # decides scale. Entries only, and never for self-sizing
                # portfolio legs. The risk engine still judges the result.
                priced = bars[-1].close
                sized_intents = []
                for raw in intents:
                    result = size_intent(
                        raw,
                        profile,
                        priced,
                        self_sized=getattr(strategy, "self_sized", False),
                        persona=persona_of.get(row.id),
                    )
                    if result.dropped:
                        summary[f"{symbol}:{row.name}"] = f"skipped:{result.reason}"
                        continue
                    sized_intents.append(result.intent)
                sized_intents, capped = cap_open_positions(
                    sized_intents, profile, set(positions)
                )
                for reason in capped:
                    log.info("intent_capped", detail=reason)
                    summary[f"{symbol}:{row.name}"] = f"skipped:{reason}"

                for intent in sized_intents:
                    enriched = intent.model_copy(
                        update={
                            "mode": mode,
                            "strategy_id": row.id,
                            "model_version_id": (
                                allocator.model_version.id
                                if getattr(allocator, "model_version", None)
                                else None
                            ),
                            "context": {**mkt, **intent.context},
                        }
                    )
                    decision = await execution.execute_intent(session, enriched)
                    summary[f"{symbol}:{row.name}"] = f"{mode}:{decision.status}"
        await session.commit()
    return summary


# ---------------------------------------------------------------- lifecycle crons


async def sync_orders(ctx: dict) -> int:
    if not _broker_configured(ctx):
        return 0
    async with ctx["session_factory"]() as session:
        updated = await ctx["execution"].sync_orders(session)
        await session.commit()
        return updated


async def snapshot_positions(ctx: dict) -> str:
    if not _broker_configured(ctx):
        return 'skipped'
    async with ctx["session_factory"]() as session:
        snap = await ctx["execution"].snapshot_positions(session)
        await session.commit()
        return str(snap.equity)


async def reconcile_orders(ctx: dict) -> dict:
    if not _broker_configured(ctx):
        return {'note': 'skipped: no broker credentials'}
    from app.execution.reconciliation import reconcile

    async with ctx["session_factory"]() as session:
        result = await reconcile(session, ctx["execution"].broker)
        await session.commit()
        return result


async def refresh_market_data(ctx: dict) -> int:
    from app.marketdata.alpaca_data import AlpacaDataProvider, store_bars

    settings = ctx["settings"]
    if not settings.alpaca_paper_api_key:
        return 0
    provider: AlpacaDataProvider = ctx["data_provider"]
    stored = 0
    async with ctx["session_factory"]() as session:
        for symbol in sorted(settings.symbol_allowlist() or {"SPY"}):
            try:
                bar = await provider.get_latest_bar(symbol, STRATEGY_TIMEFRAME)
            except Exception as exc:
                log.warning("market_data_refresh_failed", symbol=symbol, error=str(exc))
                continue
            if bar is not None:
                stored += await store_bars(session, [bar])
        await session.commit()
    return stored


# ---------------------------------------------------------------- learning crons


async def evaluate_outcomes(ctx: dict) -> int:
    from app.learning.evaluation import evaluate_decision_outcomes

    async with ctx["session_factory"]() as session:
        n = await evaluate_decision_outcomes(session, now=utcnow())
        await session.commit()
        return n


async def train_allocator_job(ctx: dict) -> str:
    from app.learning.training import train_allocator

    async with ctx["session_factory"]() as session:
        try:
            model = await train_allocator(session, now=utcnow())
        except ValueError as exc:
            log.info("training_skipped", reason=str(exc))
            return "skipped"
        await session.commit()
        log.info(
            "allocator_trained",
            model=model.name,
            version=model.version,
            status=str(model.status),  # REGISTERED — inert until a human promotes it
        )
        return f"{model.name} v{model.version}"
