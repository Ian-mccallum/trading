"""TradingView webhook ingestion: parse, authenticate, dedupe, and persist alerts.

Security and durability model:

- The endpoint is dead unless ``tradingview_webhook_secret`` is configured;
  there is no "open" mode.
- The shared secret travels in the JSON payload (TradingView cannot set custom
  headers). It is compared with ``hmac.compare_digest`` and stripped from
  everything we persist or log: stored payloads always carry
  ``secret: "[redacted]"``, and neither the provided nor the expected secret
  ever reaches a log line.
- Header-capable senders can additionally be required to sign the raw request
  body with HMAC-SHA256 (``webhook_hmac_secret`` + ``X-Signature`` header).
- Every parseable outcome is persisted as a ``Signal`` row (validated /
  rejected / expired) so the audit trail includes hostile traffic. Auth-failed
  rows get a random dedupe key so an attacker probing with a guessed
  ``signal_id`` cannot occupy the dedupe slot of the legitimate alert.
- Dedupe relies on the (source, dedupe_key) unique constraint as the source of
  truth; inserts run inside a SAVEPOINT (``begin_nested``) so a concurrent
  duplicate delivery (TradingView retries) surfaces as a handled
  ``IntegrityError`` instead of poisoning the session. Duplicates return the
  existing row with HTTP 200 so retries never look like errors to the sender.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.db.models import Signal, SignalSource, SignalStatus
from app.logging import get_logger

log = get_logger("webhooks.tradingview")

REDACTED = "[redacted]"
SIGNATURE_HEADER = "X-Signature"
_SOURCE = SignalSource.TRADINGVIEW.value


class TradingViewAlert(BaseModel):
    """Inbound TradingView alert payload. Extra fields are preserved
    (``extra='allow'``) so custom alert-template fields survive into the
    stored payload."""

    model_config = ConfigDict(extra="allow")

    secret: str
    symbol: str
    action: Literal["buy", "sell", "close"]
    qty: Decimal | None = None
    price: Decimal | None = None
    time: datetime | None = None
    signal_id: str | None = None
    strategy: str | None = None
    comment: str | None = None


def normalize_symbol(symbol: str) -> str:
    """Uppercase and strip any exchange prefix: ``'NASDAQ:AAPL' -> 'AAPL'``."""
    sym = symbol.strip().upper()
    if ":" in sym:
        sym = sym.rsplit(":", 1)[-1]
    return sym.strip()


# ---------------------------------------------------------------- helpers


def _fallback_dedupe_key(
    symbol: str, action: str, qty: Decimal | None, received_at: datetime
) -> str:
    """Content hash used when the sender supplies no ``signal_id``: identical
    alerts landing within the same minute collapse to one signal."""
    bucket = received_at.astimezone(UTC).replace(second=0, microsecond=0).isoformat()
    material = "|".join((symbol, action, str(qty) if qty is not None else "", bucket))
    return hashlib.sha256(material.encode()).hexdigest()


def _header_lookup(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup that works for Starlette Headers and
    plain dicts alike."""
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    for key, val in headers.items():
        if key.lower() == lowered:
            return val
    return None


def _hmac_signature_valid(secret: str, raw_body: bytes, headers: Mapping[str, str]) -> bool:
    provided = _header_lookup(headers, SIGNATURE_HEADER)
    if not provided:
        return False
    candidate = provided.strip().lower().removeprefix("sha256=")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(candidate, expected)


async def _store_signal(
    session: AsyncSession,
    *,
    dedupe_key: str,
    symbol: str,
    action: str,
    status: SignalStatus,
    payload: dict[str, Any],
    status_reason: str | None = None,
    signal_ts: datetime | None = None,
) -> tuple[Signal, bool]:
    """Insert a Signal inside a SAVEPOINT and commit. On a (source, dedupe_key)
    unique violation — a concurrent or repeated delivery — roll back to the
    savepoint and return the already-stored row. Returns ``(row, created)``."""
    row = Signal(
        source=_SOURCE,
        dedupe_key=dedupe_key,
        symbol=symbol,
        action=action,
        status=status,
        status_reason=status_reason,
        payload=payload,
        signal_ts=signal_ts,
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        existing = (
            await session.execute(
                sa.select(Signal).where(
                    Signal.source == _SOURCE, Signal.dedupe_key == dedupe_key
                )
            )
        ).scalar_one_or_none()
        if existing is None:  # not a dedupe collision — genuine integrity failure
            raise
        return existing, False
    await session.commit()
    return row, True


# ---------------------------------------------------------------- pipeline


async def validate_and_store(
    session: AsyncSession,
    settings: Settings,
    raw_body: bytes,
    headers: Mapping[str, str],
) -> tuple[Signal | None, str, int]:
    """Full ingestion pipeline: configuration gate, parse, authenticate,
    freshness, dedupe, persist. Returns ``(signal_or_none, detail, http_status)``."""
    # 1. Refuse everything until a secret is configured — never an open endpoint.
    if not settings.tradingview_webhook_secret:
        log.warning("webhook_not_configured")
        return None, "webhook not configured", 503

    # 2. Parse JSON + schema. Unparseable input is not persisted at all.
    try:
        raw = json.loads(raw_body)
        alert = TradingViewAlert.model_validate(raw)
    except ValueError:  # includes json.JSONDecodeError and pydantic.ValidationError
        log.warning("webhook_malformed_payload", body_bytes=len(raw_body))
        return None, "invalid payload", 422

    payload: dict[str, Any] = {**raw, "secret": REDACTED}
    symbol = normalize_symbol(alert.symbol)
    action = alert.action
    signal_ts = alert.time
    if signal_ts is not None and signal_ts.tzinfo is None:
        signal_ts = signal_ts.replace(tzinfo=UTC)
    received_at = utcnow()

    # 3. Payload passphrase, constant-time. 4. Optional raw-body HMAC header.
    authorized = hmac.compare_digest(
        alert.secret.encode(), settings.tradingview_webhook_secret.encode()
    )
    if authorized and settings.webhook_hmac_secret:
        authorized = _hmac_signature_valid(settings.webhook_hmac_secret, raw_body, headers)
    if not authorized:
        row, _ = await _store_signal(
            session,
            # Random key: rejected traffic must never occupy a legitimate
            # alert's dedupe slot (and repeated probes must not collide).
            dedupe_key=f"auth-failed:{uuid.uuid4().hex}",
            symbol=symbol,
            action=action,
            status=SignalStatus.REJECTED,
            status_reason="auth_failed",
            payload=payload,
            signal_ts=signal_ts,
        )
        log.warning("webhook_auth_failed", symbol=symbol)
        return row, "authentication failed", 401

    dedupe_key = (alert.signal_id or "").strip()[:128] or _fallback_dedupe_key(
        symbol, action, alert.qty, received_at
    )

    # 5. Freshness: reject alerts older than the configured window.
    if signal_ts is not None:
        age = (received_at - signal_ts).total_seconds()
        max_age = settings.webhook_max_signal_age_seconds
        if age > max_age:
            row, _ = await _store_signal(
                session,
                dedupe_key=dedupe_key,
                symbol=symbol,
                action=action,
                status=SignalStatus.EXPIRED,
                status_reason=f"signal age {int(age)}s exceeds {max_age}s",
                payload=payload,
                signal_ts=signal_ts,
            )
            log.warning("webhook_signal_expired", symbol=symbol, age_seconds=int(age))
            return row, "signal expired", 422

    # 6. Dedupe: TradingView retries must not error, so duplicates return the
    # existing row with a 200.
    existing = (
        await session.execute(
            sa.select(Signal).where(
                Signal.source == _SOURCE, Signal.dedupe_key == dedupe_key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        log.info("webhook_duplicate_signal", symbol=symbol, signal_id=str(existing.id))
        return existing, "duplicate", 200

    # 7. Store the validated signal; a lost insert race is a duplicate too.
    row, created = await _store_signal(
        session,
        dedupe_key=dedupe_key,
        symbol=symbol,
        action=action,
        status=SignalStatus.VALIDATED,
        payload=payload,
        signal_ts=signal_ts,
    )
    if not created:
        log.info("webhook_duplicate_signal", symbol=symbol, signal_id=str(row.id))
        return row, "duplicate", 200

    log.info(
        "webhook_signal_validated", symbol=symbol, action=action, signal_id=str(row.id)
    )
    return row, "validated", 202
