"""Application configuration.

Safety model for environment separation:

- ``trading_env`` selects paper vs live. It defaults to ``paper``.
- Paper and live credentials are *separate* settings fields; there is no shared
  "API key" that could silently point at the wrong account.
- Broker base URLs are hardcoded per environment in ``app.brokers.alpaca`` and
  are NOT configurable, so a config typo cannot route paper orders to the live
  endpoint or vice versa.
- Live trading requires ALL of the following, checked by ``live_trading_allowed()``:
    1. ``trading_env == "live"``
    2. ``live_trading_enabled`` is true (explicit opt-in flag)
    3. ``live_trading_confirmation`` equals the exact acknowledgement phrase
    4. live credentials are present
  and, at runtime, a database-level arm switch (``system_controls`` row) that an
  operator must set via the admin API. Absent any of these, the platform runs
  paper-only.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

LIVE_CONFIRMATION_PHRASE = "I-UNDERSTAND-LIVE-TRADING-RISKS"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "quantplatform"
    debug: bool = False

    # Token required (X-Admin-Token header) for all /admin endpoints.
    # Empty token = admin API disabled entirely; there is no unauthenticated admin access.
    admin_api_token: str = ""

    # --- Database / queue ---
    database_url: str = "postgresql+psycopg://trading:trading@localhost:5433/trading"
    redis_url: str = "redis://localhost:6380/0"

    # --- Environment selection ---
    trading_env: Literal["paper", "live"] = "paper"

    # --- Alpaca credentials: deliberately separate per environment ---
    alpaca_paper_api_key: str = ""
    alpaca_paper_api_secret: str = ""
    alpaca_live_api_key: str = ""
    alpaca_live_api_secret: str = ""

    # --- Live-trading gates (all must pass; see module docstring) ---
    live_trading_enabled: bool = False
    live_trading_confirmation: str = ""

    # --- Strategy context ---
    # Benchmark whose trailing returns are injected into strategy features for
    # relative-strength criteria. Empty disables benchmark injection entirely
    # (consumers then skip those criteria and flag that they did).
    strategy_benchmark_symbol: str = "SPY"

    # --- Webhook ingestion ---
    tradingview_webhook_secret: str = ""  # shared passphrase carried in payload
    webhook_hmac_secret: str = ""  # optional HMAC-SHA256 for header-capable senders
    webhook_max_signal_age_seconds: int = 120

    # --- Risk defaults (DB-backed limits override these; see app.risk) ---
    # Sized to be coherent with profile.toml's default capital_allocation of
    # $25,000: the largest leg any shipped persona wants is All Weather's 40%
    # bond sleeve ($10,000). Raise these together with capital_allocation, or
    # the engine will correctly reject the resulting orders. They remain an
    # independent ceiling — the profile cannot widen them.
    risk_max_order_notional: float = 11_000.0
    risk_max_position_notional: float = 11_000.0
    risk_max_gross_exposure: float = 26_000.0
    risk_max_daily_loss: float = 500.0
    risk_max_drawdown_pct: float = 10.0
    risk_duplicate_window_seconds: int = 60
    # Stale-data guard. MUST be matched to the bar timeframe being traded: on
    # daily bars the freshest possible price is the last session's close, so a
    # short intraday-style limit rejects every order forever. 3 days covers a
    # long weekend. TIGHTEN THIS to minutes if intraday trading is ever added.
    risk_max_data_age_seconds: int = 259_200
    risk_circuit_breaker_errors: int = 5
    risk_circuit_breaker_window_seconds: int = 300
    risk_symbol_allowlist: str = ""  # comma-separated; empty = allow any equity symbol

    def symbol_allowlist(self) -> frozenset[str]:
        return frozenset(
            s.strip().upper() for s in self.risk_symbol_allowlist.split(",") if s.strip()
        )

    def live_trading_allowed(self) -> bool:
        """Static (config-level) live gate. The runtime DB arm switch is checked
        separately in the execution path; both must pass."""
        return (
            self.trading_env == "live"
            and self.live_trading_enabled
            and self.live_trading_confirmation == LIVE_CONFIRMATION_PHRASE
            and bool(self.alpaca_live_api_key)
            and bool(self.alpaca_live_api_secret)
        )

    def broker_credentials(self) -> tuple[str, str]:
        """Credentials for the *active* environment. Raises if live is selected
        but not fully gated — there is no fallback from live to paper creds."""
        if self.trading_env == "live":
            if not self.live_trading_allowed():
                raise RuntimeError(
                    "trading_env=live but the live-trading gates are not satisfied; "
                    "refusing to construct live credentials"
                )
            return (self.alpaca_live_api_key, self.alpaca_live_api_secret)
        return (self.alpaca_paper_api_key, self.alpaca_paper_api_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
