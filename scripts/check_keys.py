"""Verify Alpaca credentials and market-data access before wiring anything up.

Answers, in order, the three questions that actually block you:

1. Are keys present in .env at all, and for which environment?
2. Do they authenticate against the trading API, and what does the account
   look like?
3. Do they work for *market data* (a separate host — keys can pass one and
   fail the other, which is confusing to debug by hand)?

Read-only: it never places, cancels, or modifies anything.

Usage: .venv/bin/python -m scripts.check_keys
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app.brokers.alpaca import PAPER_TRADING_URL, AlpacaBroker, make_alpaca_broker
from app.brokers.base import BrokerError
from app.config import get_settings
from app.db.base import utcnow
from app.db.models import Environment
from app.marketdata.alpaca_data import AlpacaDataProvider

OK, BAD, WARN = "  ✅", "  ❌", "  ⚠️ "


def _mask(value: str) -> str:
    if not value:
        return "(empty)"
    return f"{value[:4]}…{value[-2:]} ({len(value)} chars)"


async def main() -> int:
    settings = get_settings()
    print("\n=== 1. Configuration ===")
    print(f"  TRADING_ENV      : {settings.trading_env}")
    print(f"  paper key        : {_mask(settings.alpaca_paper_api_key)}")
    print(f"  paper secret     : {_mask(settings.alpaca_paper_api_secret)}")
    print(f"  live gates pass  : {settings.live_trading_allowed()}  (expected False)")

    if not settings.alpaca_paper_api_key or not settings.alpaca_paper_api_secret:
        print(f"{BAD} No paper credentials in .env.")
        print("     Set ALPACA_PAPER_API_KEY and ALPACA_PAPER_API_SECRET, then rerun.")
        return 1

    if settings.alpaca_paper_api_key.startswith("AK"):
        print(f"{WARN} Key starts with 'AK', which is a LIVE key prefix.")
        print("     Paper keys usually start with 'PK'. Make sure you switched the")
        print("     dashboard to 'Paper Trading' BEFORE generating keys.")

    # --- 2. Trading API -------------------------------------------------
    print("\n=== 2. Trading API ===")
    print(f"  host: {PAPER_TRADING_URL}")
    broker: AlpacaBroker = make_alpaca_broker(settings)
    if broker.environment != Environment.PAPER:
        print(f"{BAD} Broker built for {broker.environment}, expected paper. Aborting.")
        return 1
    try:
        account = await broker.get_account()
    except BrokerError as exc:
        print(f"{BAD} {exc}")
        print("     401 => key/secret wrong, or generated for the LIVE account.")
        print("     403 => key lacks permission; regenerate from the Paper dashboard.")
        await broker.aclose()
        return 1
    print(f"{OK} Authenticated.")
    print(f"     equity       : {account.equity}")
    print(f"     cash         : {account.cash}")
    print(f"     buying power : {account.buying_power}")

    positions = await broker.get_positions()
    print(f"{OK} Positions readable: {len(positions)} open")
    await broker.aclose()

    # --- 3. Market data (different host, same keys) ----------------------
    print("\n=== 3. Market data API ===")
    provider = AlpacaDataProvider(
        settings.alpaca_paper_api_key, settings.alpaca_paper_api_secret
    )
    end = utcnow() - timedelta(minutes=20)  # avoid the free-tier recency window
    start = end - timedelta(days=10)
    try:
        bars = await provider.get_bars("SPY", "1Day", start, end)
    except BrokerError as exc:
        print(f"{BAD} {exc}")
        print("     Market data uses the same keys but a different host.")
        return 1
    if not bars:
        print(f"{WARN} Authenticated but no SPY bars returned for the window.")
        print("     Often just market holidays — try --days 30 in scripts.backfill_bars.")
    else:
        latest = bars[-1]
        print(f"{OK} Fetched {len(bars)} SPY daily bars.")
        print(f"     latest: {latest.ts.date()} close={latest.close}")

    print("\n=== Ready ===")
    print("  Next: .venv/bin/python -m scripts.backfill_bars SPY QQQ VTI TLT GLD --days 730")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
