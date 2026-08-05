"""Backtesting engine: replays historical bars through a strategy against the
``SimulatedBroker`` with strict no-lookahead semantics.

Event loop, per bar ``i``:

1. Advance the simulated clock/price to bar ``i``'s OPEN and execute the
   intents the strategy emitted on bar ``i-1`` as market orders — a decision
   made on a bar can only ever fill at the *next* bar's open.
2. Move the price to bar ``i``'s CLOSE and record an equity-curve point.
3. If ``i >= strategy.warmup_bars()``, build a ``StrategyContext`` whose
   ``bars`` slice is ``bars[:i+1]`` (nothing after bar ``i`` is visible) and
   queue the returned intents for the next open. Intents emitted on the final
   bar are discarded — there is no next open to fill at.

The strategy runs the exact same ``on_bars`` code path used live; ``close``
intents flatten the entire current position. Rejected orders (long-only or
cash-limit violations in the simulator) are skipped, not fatal — a backtest
should reveal a strategy that oversells, not crash on it.

Results are persisted via ``save_backtest_result`` as a ``PerformanceReport``
plus a completed ``Experiment``. Backtests never create ``Decision`` rows —
the decision audit spine is reserved for paper/live/shadow flow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.backtest.metrics import compute_metrics
from app.brokers.base import BrokerRejectionError
from app.brokers.simulated import SimulatedBroker
from app.db.base import utcnow
from app.db.models import (
    DecisionMode,
    Environment,
    Experiment,
    ExperimentKind,
    ExperimentStatus,
    PerformanceReport,
)
from app.db.models import (
    Strategy as StrategyRow,
)
from app.logging import get_logger
from app.schemas.core import BarData, OrderRequest, OrderSide, OrderType, TradeIntent
from app.strategies.base import Strategy, StrategyContext

log = get_logger("backtest.engine")


@dataclass
class BacktestConfig:
    initial_cash: Decimal = Decimal("100000")
    slippage_bps: Decimal = Decimal(0)
    commission_per_share: Decimal = Decimal(0)
    timeframe: str = "1Day"


@dataclass
class BacktestResult:
    equity_curve: list[tuple[datetime, Decimal]]
    trades: list[dict[str, Any]]  # ts / symbol / side / qty / price
    final_equity: Decimal
    metrics: dict[str, Any]
    config: BacktestConfig = field(default_factory=BacktestConfig)


class BacktestEngine:
    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()

    async def run(self, strategy: Strategy, bars: list[BarData]) -> BacktestResult:
        """Replay ``bars`` (oldest → newest, single symbol) through
        ``strategy``. See module docstring for the no-lookahead loop."""
        config = self.config
        if not bars:
            return BacktestResult(
                equity_curve=[],
                trades=[],
                final_equity=config.initial_cash,
                metrics=compute_metrics([], []),
                config=config,
            )

        symbol = bars[0].symbol
        broker = SimulatedBroker(
            initial_cash=config.initial_cash,
            slippage_bps=config.slippage_bps,
            commission_per_share=config.commission_per_share,
            environment=Environment.PAPER,
        )
        warmup = strategy.warmup_bars()
        equity_curve: list[tuple[datetime, Decimal]] = []
        trades: list[dict[str, Any]] = []
        pending: list[TradeIntent] = []
        order_seq = 0

        for i, bar in enumerate(bars):
            # 1. Fill last bar's intents at this bar's open.
            broker.set_clock(bar.ts)
            broker.set_price(symbol, bar.open)
            for intent in pending:
                order_seq += 1
                trade = await self._execute(broker, symbol, intent, bar.ts, order_seq)
                if trade is not None:
                    trades.append(trade)
            pending = []

            # 2. Mark to this bar's close and record equity.
            broker.set_price(symbol, bar.close)
            account = await broker.get_account()
            equity_curve.append((bar.ts, account.equity))

            # 3. Let the strategy decide on bars[:i+1]; fills happen next open.
            if i >= warmup:
                positions = await broker.get_positions()
                position = next((p for p in positions if p.symbol == symbol), None)
                ctx = StrategyContext(
                    symbol=symbol,
                    bars=bars[: i + 1],
                    position=position,
                    equity=account.equity,
                    features={},
                )
                pending = list(strategy.on_bars(ctx))
        # Intents from the final bar (still in `pending`) are discarded.

        final_equity = equity_curve[-1][1]
        return BacktestResult(
            equity_curve=equity_curve,
            trades=trades,
            final_equity=final_equity,
            metrics=compute_metrics(equity_curve, trades),
            config=config,
        )

    @staticmethod
    async def _execute(
        broker: SimulatedBroker,
        symbol: str,
        intent: TradeIntent,
        ts: datetime,
        order_seq: int,
    ) -> dict[str, Any] | None:
        """Submit one intent as a market order at the current (open) price.
        Returns a trade record, or None if skipped/rejected."""
        action = str(intent.action)
        if action == "close":
            positions = await broker.get_positions()
            held = next((p.qty for p in positions if p.symbol == symbol), Decimal(0))
            if held <= 0:
                return None
            side, qty = OrderSide.SELL, held
        elif action == "sell":
            side, qty = OrderSide.SELL, intent.qty
        else:
            side, qty = OrderSide.BUY, intent.qty

        request = OrderRequest(
            client_order_id=f"bt-{order_seq}",
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=OrderType.MARKET,
        )
        try:
            state = await broker.submit_order(request)
        except BrokerRejectionError as exc:
            log.warning("backtest_order_rejected", symbol=symbol, side=str(side), error=str(exc))
            return None
        return {
            "ts": ts,
            "symbol": symbol,
            "side": str(side),
            "qty": state.filled_qty,
            "price": state.filled_avg_price,
        }


async def save_backtest_result(
    session: AsyncSession,
    strategy_row: StrategyRow,
    result: BacktestResult,
    period_start: datetime,
    period_end: datetime,
) -> tuple[PerformanceReport, Experiment]:
    """Persist a backtest as a ``PerformanceReport`` + completed
    ``Experiment``. No ``Decision`` rows are written for backtests."""
    config = result.config
    report = PerformanceReport(
        strategy_id=strategy_row.id,
        environment=Environment.PAPER,
        mode=DecisionMode.BACKTEST,
        period_start=period_start,
        period_end=period_end,
        metrics=dict(result.metrics),
    )
    experiment = Experiment(
        name=f"backtest:{strategy_row.name}:v{strategy_row.version}",
        kind=ExperimentKind.BACKTEST,
        status=ExperimentStatus.COMPLETED,
        config={
            "initial_cash": str(config.initial_cash),
            "slippage_bps": str(config.slippage_bps),
            "commission_per_share": str(config.commission_per_share),
            "timeframe": config.timeframe,
            "strategy_id": str(strategy_row.id),
        },
        results={
            **result.metrics,
            "summary": {
                "final_equity": float(result.final_equity),
                "num_trades": len(result.trades),
                "num_bars": len(result.equity_curve),
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
            },
        },
        completed_at=utcnow(),
    )
    session.add(report)
    session.add(experiment)
    await session.flush()
    log.info(
        "backtest_saved",
        strategy=strategy_row.name,
        version=strategy_row.version,
        final_equity=str(result.final_equity),
    )
    return report, experiment
