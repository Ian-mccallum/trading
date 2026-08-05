"""Alpaca broker adapter (v2 trading REST API).

Environment separation guarantee: the paper and live hosts are hardcoded
module constants and deliberately NOT configurable — no setting, env var, or
constructor argument can point a paper broker at the live API or vice versa.
Each ``AlpacaBroker`` is locked to one ``Environment`` at construction and
derives its base URL solely from that.

Reliability model:

- GET requests retry up to 3 attempts with short exponential backoff on
  429/5xx/transport failures.
- Order submission (POST) is attempted exactly once. ``client_order_id``
  makes caller-side retries safe: if Alpaca answers 422 "client_order_id must
  be unique", the existing order is fetched by that id and returned instead of
  raising, so a resubmission can never create a second broker order.
- Other 4xx responses raise ``BrokerRejectionError``; 5xx / timeouts /
  transport errors raise ``BrokerUnavailableError``.

``app.marketdata.alpaca_data`` reuses the transport/parse helpers defined here
(``request_with_retry``, ``raise_for_client_error``, ``to_decimal``,
``parse_timestamp``).
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from app.brokers.base import Broker, BrokerRejectionError, BrokerUnavailableError
from app.config import Settings
from app.db.models import Environment, OrderSide, OrderStatus, OrderType
from app.logging import get_logger
from app.schemas.core import (
    AccountState,
    FillData,
    OrderRequest,
    OrderState,
    PositionState,
)

log = get_logger("brokers.alpaca")

# Hardcoded on purpose: the paper/live separation must not be configurable.
PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
LIVE_TRADING_URL = "https://api.alpaca.markets"

DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_GET_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.1

# Alpaca's error code for "client_order_id must be unique".
DUPLICATE_CLIENT_ORDER_ID_CODE = 40010001

# Alpaca order status -> platform OrderStatus. Unknown/rare statuses fall back
# to SUBMITTED (non-terminal), so reconciliation keeps polling them.
_STATUS_MAP: dict[str, OrderStatus] = {
    "new": OrderStatus.ACCEPTED,
    "accepted": OrderStatus.ACCEPTED,
    "pending_new": OrderStatus.SUBMITTED,
    "accepted_for_bidding": OrderStatus.SUBMITTED,
    "held": OrderStatus.SUBMITTED,
    "suspended": OrderStatus.SUBMITTED,
    "calculated": OrderStatus.ACCEPTED,
    "stopped": OrderStatus.ACCEPTED,
    "done_for_day": OrderStatus.ACCEPTED,
    "pending_cancel": OrderStatus.ACCEPTED,
    "pending_replace": OrderStatus.ACCEPTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "replaced": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}


def map_order_status(raw_status: str) -> OrderStatus:
    """Normalize an Alpaca order status string to the platform enum."""
    return _STATUS_MAP.get(raw_status, OrderStatus.SUBMITTED)


# ---------------------------------------------------------------- parse helpers

_EXCESS_FRACTION_RE = re.compile(r"\.(\d{6})\d+")


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an Alpaca RFC3339 timestamp; trims sub-microsecond precision."""
    if not value:
        return None
    trimmed = _EXCESS_FRACTION_RE.sub(r".\1", value.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(trimmed)
    except ValueError:
        return None


def to_decimal(value: Any) -> Decimal | None:
    """Convert an Alpaca numeric field (string or JSON number) to Decimal."""
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _parse_order(data: dict[str, Any]) -> OrderState:
    try:
        order_type = OrderType(str(data.get("type", "")))
    except ValueError:
        # Order types this platform never submits (stop, trailing_stop, ...).
        order_type = OrderType.MARKET
    return OrderState(
        client_order_id=str(data.get("client_order_id", "")),
        broker_order_id=str(data["id"]) if data.get("id") else None,
        symbol=str(data.get("symbol", "")),
        side=OrderSide(str(data.get("side", "buy"))),
        qty=to_decimal(data.get("qty")) or Decimal(0),
        order_type=order_type,
        status=map_order_status(str(data.get("status", ""))),
        filled_qty=to_decimal(data.get("filled_qty")) or Decimal(0),
        filled_avg_price=to_decimal(data.get("filled_avg_price")),
        submitted_at=parse_timestamp(data.get("submitted_at"))
        or parse_timestamp(data.get("created_at")),
        raw=data,
    )


def _parse_position(data: dict[str, Any]) -> PositionState:
    qty = to_decimal(data.get("qty")) or Decimal(0)
    if data.get("side") == "short" and qty > 0:
        qty = -qty
    return PositionState(
        symbol=str(data["symbol"]),
        qty=qty,
        avg_entry_price=to_decimal(data.get("avg_entry_price")) or Decimal(0),
        market_value=to_decimal(data.get("market_value")) or Decimal(0),
        unrealized_pl=to_decimal(data.get("unrealized_pl")) or Decimal(0),
    )


# ---------------------------------------------------------------- transport


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    attempts: int = MAX_GET_ATTEMPTS,
) -> httpx.Response:
    """Issue a request, retrying 429/5xx/transport failures with short
    exponential backoff. Non-idempotent callers pass ``attempts=1``.

    4xx responses (other than 429) are returned to the caller for
    interpretation — the idempotent-submit and 404 paths need the body.
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        reason: str
        try:
            response = await client.request(
                method, url, headers=headers, params=params, json=json_body
            )
        except httpx.HTTPError as exc:
            last_error = exc
            reason = f"transport error: {exc!r}"
        else:
            if response.status_code != 429 and response.status_code < 500:
                return response
            reason = f"HTTP {response.status_code}"
        if attempt + 1 < attempts:
            delay = BACKOFF_BASE_SECONDS * (2**attempt)
            log.warning(
                "alpaca_request_retry",
                method=method,
                url=url,
                attempt=attempt + 1,
                reason=reason,
                retry_in=delay,
            )
            await asyncio.sleep(delay)
            continue
        raise BrokerUnavailableError(
            f"{method} {url} failed after {attempts} attempt(s): {reason}"
        ) from last_error
    raise BrokerUnavailableError(f"{method} {url}: no attempts made")  # pragma: no cover


def raise_for_client_error(response: httpx.Response) -> None:
    """Map any remaining 4xx to BrokerRejectionError (5xx/429 never reach
    here; ``request_with_retry`` already converted them)."""
    if 400 <= response.status_code < 500:
        try:
            body = response.json()
            message = body.get("message", "") if isinstance(body, dict) else str(body)
        except ValueError:
            message = response.text[:200]
        raise BrokerRejectionError(f"HTTP {response.status_code}: {message}")


def _is_duplicate_client_order_id(response: httpx.Response) -> bool:
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    message = str(body.get("message", "")).lower()
    return body.get("code") == DUPLICATE_CLIENT_ORDER_ID_CODE or (
        "client_order_id" in message and "unique" in message
    )


# ---------------------------------------------------------------- broker


class AlpacaBroker(Broker):
    """Alpaca trading API adapter, locked to one environment."""

    def __init__(
        self,
        environment: Environment,
        api_key: str,
        api_secret: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.environment = environment
        self._base_url = (
            PAPER_TRADING_URL if environment == Environment.PAPER else LIVE_TRADING_URL
        )
        self._headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": api_secret}
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        return await request_with_retry(
            self._client, "GET", f"{self._base_url}{path}", headers=self._headers, params=params
        )

    # ------------------------------------------------------------ account

    async def get_account(self) -> AccountState:
        response = await self._get("/v2/account")
        raise_for_client_error(response)
        data = response.json()
        return AccountState(
            equity=to_decimal(data.get("equity")) or Decimal(0),
            cash=to_decimal(data.get("cash")) or Decimal(0),
            buying_power=to_decimal(data.get("buying_power")) or Decimal(0),
            environment=self.environment,
        )

    async def get_positions(self) -> list[PositionState]:
        response = await self._get("/v2/positions")
        raise_for_client_error(response)
        return [_parse_position(item) for item in response.json()]

    # ------------------------------------------------------------ orders

    async def submit_order(self, request: OrderRequest) -> OrderState:
        payload: dict[str, Any] = {
            "client_order_id": request.client_order_id,
            "symbol": request.symbol,
            "side": request.side.value,
            "qty": str(request.qty),
            "type": request.order_type.value,
            "time_in_force": request.time_in_force,
        }
        if request.limit_price is not None:
            payload["limit_price"] = str(request.limit_price)

        # Exactly one attempt: client_order_id idempotency (below) makes a
        # caller-level retry safe even when the outcome of this POST is unknown.
        response = await request_with_retry(
            self._client,
            "POST",
            f"{self._base_url}/v2/orders",
            headers=self._headers,
            json_body=payload,
            attempts=1,
        )
        if response.status_code == 422 and _is_duplicate_client_order_id(response):
            log.info(
                "duplicate_client_order_id_resolved",
                client_order_id=request.client_order_id,
                symbol=request.symbol,
            )
            existing = await self.get_order(request.client_order_id)
            if existing is not None:
                return existing
        raise_for_client_error(response)
        state = _parse_order(response.json())
        log.info(
            "order_submitted_to_alpaca",
            client_order_id=state.client_order_id,
            symbol=state.symbol,
            status=str(state.status),
            environment=str(self.environment),
        )
        return state

    async def get_order(self, client_order_id: str) -> OrderState | None:
        response = await self._get(
            "/v2/orders:by_client_order_id", params={"client_order_id": client_order_id}
        )
        if response.status_code == 404:
            return None
        raise_for_client_error(response)
        return _parse_order(response.json())

    async def cancel_order(self, client_order_id: str) -> None:
        state = await self.get_order(client_order_id)
        if state is None or not state.broker_order_id:
            raise BrokerRejectionError(
                f"cannot cancel: no order found for client_order_id {client_order_id!r}"
            )
        response = await request_with_retry(
            self._client,
            "DELETE",
            f"{self._base_url}/v2/orders/{state.broker_order_id}",
            headers=self._headers,
            attempts=1,
        )
        if response.status_code == 404:
            return  # already gone at the broker; treat as canceled
        raise_for_client_error(response)

    async def list_open_orders(self) -> list[OrderState]:
        response = await self._get("/v2/orders", params={"status": "open", "limit": 500})
        raise_for_client_error(response)
        return [_parse_order(item) for item in response.json()]

    # ------------------------------------------------------------ fills

    async def list_fills(self, since: datetime) -> list[FillData]:
        response = await self._get(
            "/v2/account/activities/FILL", params={"after": since.isoformat()}
        )
        raise_for_client_error(response)
        fills: list[FillData] = []
        for item in response.json():
            filled_at = parse_timestamp(item.get("transaction_time"))
            if filled_at is None:
                log.warning("fill_activity_missing_timestamp", activity_id=item.get("id"))
                continue
            fills.append(
                FillData(
                    broker_fill_id=str(item["id"]),
                    broker_order_id=str(item["order_id"]) if item.get("order_id") else None,
                    client_order_id=None,  # activities carry only the broker order id
                    qty=to_decimal(item.get("qty")) or Decimal(0),
                    price=to_decimal(item.get("price")) or Decimal(0),
                    filled_at=filled_at,
                )
            )
        return fills


# ---------------------------------------------------------------- factory


def make_alpaca_broker(settings: Settings) -> AlpacaBroker:
    """Build a broker for the configured environment.

    ``Settings.broker_credentials`` refuses ungated live configurations, so
    this cannot construct a live broker unless every static gate passes.
    """
    api_key, api_secret = settings.broker_credentials()
    return AlpacaBroker(
        environment=Environment(settings.trading_env),
        api_key=api_key,
        api_secret=api_secret,
    )
