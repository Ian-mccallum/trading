"""Strategy interface.

A strategy is a pure decision function over market data: it receives bars and
account context and returns ``TradeIntent`` proposals. It has NO access to a
broker, the database session, the risk engine, or order submission — the
execution service owns all of that. This is what makes strategies
interchangeable and safely evaluable (live, paper, shadow, or backtest use the
identical code path).

Concrete strategies subclass ``Strategy``, are registered in
``app.strategies.registry``, and are instantiated from a DB ``Strategy`` row
(class_path + params). New parameterizations are new DB versions.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.schemas.core import BarData, PositionState, TradeIntent


@dataclass(frozen=True)
class StrategyContext:
    """Everything a strategy may look at when deciding.

    ``bars`` is ordered oldest → newest and includes the current bar last.
    ``position`` is the current position in the symbol (None if flat).
    ``equity`` is account equity, for position sizing.
    ``features`` carries precomputed regime/indicator features, if any.
    """

    symbol: str
    bars: list[BarData]
    position: PositionState | None
    equity: Decimal
    #: Capital the owner profile allocates to the bot, when set. Strategies
    #: that construct portfolios (target-weight sleeves) must size against
    #: THIS rather than ``equity``: a 25% leg means a quarter of the money the
    #: bot was given, not a quarter of the whole brokerage account. Falls back
    #: to ``equity`` when absent, which is right for backtests.
    investable: Decimal | None = None
    features: dict[str, Any] = field(default_factory=dict)

    @property
    def capital_base(self) -> Decimal:
        """The denominator for portfolio-weight maths."""
        return self.investable if self.investable is not None else self.equity


class Strategy(abc.ABC):
    """Base class for all strategies.

    Subclasses must be deterministic given (params, context) — no I/O, no
    randomness, no wall-clock reads — so that backtests, shadow evaluation,
    and live behavior are identical.
    """

    #: Short unique name; used for registry lookup.
    name: str = "base"

    #: True when the strategy computes its own position size from portfolio
    #: context (a target-weight sleeve does). The profile's position sizer
    #: leaves these alone — re-sizing them would destroy the allocation they
    #: exist to express.
    self_sized: bool = False

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params: dict[str, Any] = dict(params or {})
        self.validate_params()

    def validate_params(self) -> None:  # noqa: B027 — optional hook, not abstract
        """Raise ValueError on bad params. Override as needed."""

    @abc.abstractmethod
    def on_bars(self, ctx: StrategyContext) -> list[TradeIntent]:
        """Return zero or more proposed trades for this context.

        Intents are proposals only; the risk engine independently approves,
        resizes, or rejects them downstream.
        """

    def warmup_bars(self) -> int:
        """Minimum number of bars required before on_bars produces output."""
        return 0
