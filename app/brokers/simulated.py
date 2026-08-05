"""Deterministic in-memory broker for backtests and unit tests.

Design:

- Fully synchronous state machine behind the async ``Broker`` interface; no
  I/O, no wall-clock reads, no uuid4. The caller drives time with
  ``set_clock(ts)`` and prices with ``set_price(symbol, price)``; order and
  fill ids come from a monotonically increasing counter, so identical inputs
  always produce identical outputs.
- Market orders fill immediately at the current price adjusted by slippage
  (buy: ``price * (1 + bps/10000)``, sell: ``price * (1 - bps/10000)``), with
  ``commission_per_share * qty`` charged against cash. Limit orders rest and
  fill at the market price once ``set_price`` crosses the limit (no slippage —
  the limit is the price guard).
- Cash account, long only: buys that would exceed available cash (including
  cash reserved by resting buy limits) and sells that would exceed held shares
  (including shares reserved by resting sell limits) raise
  ``BrokerRejectionError``. There is no shorting and no margin.
- ``submit_order`` is idempotent on ``client_order_id``: resubmitting an id
  returns the existing order state and never creates a second order or fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.brokers.base import Broker, BrokerRejectionError
from app.db.models import Environment, OrderSide, OrderStatus, OrderType
from app.logging import get_logger
from app.schemas.core import (
    AccountState,
    FillData,
    OrderRequest,
    OrderState,
    PositionState,
)

log = get_logger("brokers.simulated")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_BPS_DENOM = Decimal(10000)


@dataclass
class _Position:
    qty: Decimal
    avg_entry_price: Decimal


@dataclass
class _SimOrder:
    request: OrderRequest
    broker_order_id: str
    status: OrderStatus
    submitted_at: datetime
    filled_qty: Decimal = Decimal(0)
    filled_avg_price: Decimal | None = None
    reason: str = ""


class SimulatedBroker(Broker):
    """In-memory ``Broker`` implementation. See module docstring."""

    def __init__(
        self,
        initial_cash: Decimal,
        slippage_bps: Decimal = Decimal(0),
        commission_per_share: Decimal = Decimal(0),
        environment: Environment = Environment.PAPER,
    ) -> None:
        self.environment = environment
        self._cash = Decimal(initial_cash)
        self._slippage_bps = Decimal(slippage_bps)
        self._commission_per_share = Decimal(commission_per_share)
        self._now = _EPOCH
        self._prices: dict[str, Decimal] = {}
        self._positions: dict[str, _Position] = {}
        self._orders: dict[str, _SimOrder] = {}  # insertion-ordered by submit
        self._fills: list[FillData] = []
        self._seq = 0

    # ------------------------------------------------------------ simulation drivers

    def set_clock(self, ts: datetime) -> None:
        """Set the simulated current time (used for submitted_at/filled_at)."""
        self._now = ts

    def set_price(self, symbol: str, price: Decimal) -> None:
        """Set the current market price and fill any crossed resting limits."""
        self._prices[symbol] = Decimal(price)
        for order in list(self._orders.values()):
            if (
                order.status == OrderStatus.ACCEPTED
                and order.request.symbol == symbol
                and order.request.order_type == OrderType.LIMIT
            ):
                self._try_fill_limit(order)

    # ------------------------------------------------------------ Broker interface

    async def get_account(self) -> AccountState:
        equity = self._cash
        for symbol, pos in self._positions.items():
            equity += pos.qty * self._prices.get(symbol, pos.avg_entry_price)
        return AccountState(
            equity=equity, cash=self._cash, buying_power=self._cash,
            environment=self.environment,
        )

    async def get_positions(self) -> list[PositionState]:
        out: list[PositionState] = []
        for symbol, pos in self._positions.items():
            price = self._prices.get(symbol, pos.avg_entry_price)
            out.append(
                PositionState(
                    symbol=symbol,
                    qty=pos.qty,
                    avg_entry_price=pos.avg_entry_price,
                    market_value=pos.qty * price,
                    unrealized_pl=(price - pos.avg_entry_price) * pos.qty,
                )
            )
        return out

    async def submit_order(self, request: OrderRequest) -> OrderState:
        existing = self._orders.get(request.client_order_id)
        if existing is not None:
            return self._to_state(existing)

        if request.order_type == OrderType.LIMIT and request.limit_price is None:
            raise BrokerRejectionError("limit order requires limit_price")
        self._validate(request)

        self._seq += 1
        order = _SimOrder(
            request=request,
            broker_order_id=f"sim-ord-{self._seq}",
            status=OrderStatus.ACCEPTED,
            submitted_at=self._now,
        )
        self._orders[request.client_order_id] = order
        if request.order_type == OrderType.MARKET:
            price = self._require_price(request.symbol)
            self._fill(order, self._slipped(price, OrderSide(request.side)))
        else:
            self._try_fill_limit(order)
        return self._to_state(order)

    async def get_order(self, client_order_id: str) -> OrderState | None:
        order = self._orders.get(client_order_id)
        return self._to_state(order) if order is not None else None

    async def cancel_order(self, client_order_id: str) -> None:
        order = self._orders.get(client_order_id)
        if order is None:
            raise BrokerRejectionError(f"unknown order {client_order_id!r}")
        if order.status in OrderStatus.terminal():
            raise BrokerRejectionError(
                f"order {client_order_id!r} is terminal ({order.status}); cannot cancel"
            )
        order.status = OrderStatus.CANCELED
        order.reason = "canceled by caller"

    async def list_open_orders(self) -> list[OrderState]:
        return [
            self._to_state(o)
            for o in self._orders.values()
            if o.status not in OrderStatus.terminal()
        ]

    async def list_fills(self, since: datetime) -> list[FillData]:
        return [f for f in self._fills if f.filled_at >= since]

    # ------------------------------------------------------------ internals

    def _require_price(self, symbol: str) -> Decimal:
        price = self._prices.get(symbol)
        if price is None:
            raise BrokerRejectionError(f"no market price set for {symbol!r}")
        return price

    def _slipped(self, price: Decimal, side: OrderSide) -> Decimal:
        slip = self._slippage_bps / _BPS_DENOM
        return price * (1 + slip) if side == OrderSide.BUY else price * (1 - slip)

    def _held_qty(self, symbol: str) -> Decimal:
        pos = self._positions.get(symbol)
        return pos.qty if pos else Decimal(0)

    def _open_orders(self, side: OrderSide) -> list[_SimOrder]:
        return [
            o
            for o in self._orders.values()
            if o.status == OrderStatus.ACCEPTED and OrderSide(o.request.side) == side
        ]

    def _validate(self, request: OrderRequest) -> None:
        """Reject sells beyond held (unreserved) shares and buys beyond
        available (unreserved) cash. Raises before any state is recorded."""
        side = OrderSide(request.side)
        if side == OrderSide.SELL:
            reserved = sum(
                (o.request.qty for o in self._open_orders(OrderSide.SELL)
                 if o.request.symbol == request.symbol),
                Decimal(0),
            )
            available = self._held_qty(request.symbol) - reserved
            if request.qty > available:
                raise BrokerRejectionError(
                    f"long-only: sell {request.qty} {request.symbol} exceeds "
                    f"available {available}"
                )
            return

        if request.order_type == OrderType.MARKET:
            price = self._slipped(self._require_price(request.symbol), OrderSide.BUY)
        else:
            assert request.limit_price is not None  # checked by caller
            price = request.limit_price
        cost = price * request.qty + self._commission_per_share * request.qty
        reserved_cash = sum(
            (
                o.request.limit_price * o.request.qty
                + self._commission_per_share * o.request.qty
                for o in self._open_orders(OrderSide.BUY)
                if o.request.limit_price is not None
            ),
            Decimal(0),
        )
        if cost + reserved_cash > self._cash:
            raise BrokerRejectionError(
                f"insufficient cash: need {cost} (+{reserved_cash} reserved), "
                f"have {self._cash}"
            )

    def _try_fill_limit(self, order: _SimOrder) -> None:
        market = self._prices.get(order.request.symbol)
        limit = order.request.limit_price
        if market is None or limit is None:
            return
        side = OrderSide(order.request.side)
        if side == OrderSide.BUY and market <= limit:
            cost = market * order.request.qty + self._commission_per_share * order.request.qty
            if cost > self._cash:
                order.status = OrderStatus.REJECTED
                order.reason = "insufficient cash at fill time"
                return
            self._fill(order, market)
        elif side == OrderSide.SELL and market >= limit:
            if order.request.qty > self._held_qty(order.request.symbol):
                order.status = OrderStatus.REJECTED
                order.reason = "insufficient shares at fill time"
                return
            self._fill(order, market)

    def _fill(self, order: _SimOrder, price: Decimal) -> None:
        request = order.request
        qty = request.qty
        commission = self._commission_per_share * qty
        side = OrderSide(request.side)
        if side == OrderSide.BUY:
            self._cash -= price * qty + commission
            pos = self._positions.get(request.symbol)
            if pos is None:
                self._positions[request.symbol] = _Position(qty=qty, avg_entry_price=price)
            else:
                total = pos.qty + qty
                pos.avg_entry_price = (pos.qty * pos.avg_entry_price + qty * price) / total
                pos.qty = total
        else:
            self._cash += price * qty - commission
            pos = self._positions[request.symbol]
            pos.qty -= qty
            if pos.qty == 0:
                del self._positions[request.symbol]

        order.status = OrderStatus.FILLED
        order.filled_qty = qty
        order.filled_avg_price = price
        self._seq += 1
        self._fills.append(
            FillData(
                broker_fill_id=f"sim-fill-{self._seq}",
                broker_order_id=order.broker_order_id,
                client_order_id=request.client_order_id,
                qty=qty,
                price=price,
                filled_at=self._now,
            )
        )
        log.debug(
            "simulated_fill",
            symbol=request.symbol,
            side=str(side),
            qty=str(qty),
            price=str(price),
        )

    def _to_state(self, order: _SimOrder) -> OrderState:
        request = order.request
        return OrderState(
            client_order_id=request.client_order_id,
            broker_order_id=order.broker_order_id,
            symbol=request.symbol,
            side=OrderSide(request.side),
            qty=request.qty,
            order_type=OrderType(request.order_type),
            status=order.status,
            filled_qty=order.filled_qty,
            filled_avg_price=order.filled_avg_price,
            submitted_at=order.submitted_at,
            raw={"simulated": True, "reason": order.reason},
        )
