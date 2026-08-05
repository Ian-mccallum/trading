"""Tests for the live-trading gate matrix in app.config.Settings.

The safety model under test: live trading requires trading_env == "live", the
explicit ``live_trading_enabled`` opt-in, the *exact* confirmation phrase, and
non-empty live credentials — all at once. Any single missing gate keeps the
platform paper-only, and ``broker_credentials()`` refuses to fall back from a
half-configured live environment to paper credentials.

All Settings are constructed with ``_env_file=None`` so a local .env cannot
leak into the assertions; an autouse fixture additionally strips the relevant
process environment variables.
"""

from __future__ import annotations

import pytest

from app.config import LIVE_CONFIRMATION_PHRASE, Settings

PAPER_CREDS = {
    "alpaca_paper_api_key": "paper-key",
    "alpaca_paper_api_secret": "paper-secret",
}
LIVE_CREDS = {
    "alpaca_live_api_key": "live-key",
    "alpaca_live_api_secret": "live-secret",
}
ALL_GATES = {
    "trading_env": "live",
    "live_trading_enabled": True,
    "live_trading_confirmation": LIVE_CONFIRMATION_PHRASE,
    **LIVE_CREDS,
}

_ENV_KEYS = (
    "TRADING_ENV",
    "LIVE_TRADING_ENABLED",
    "LIVE_TRADING_CONFIRMATION",
    "ALPACA_PAPER_API_KEY",
    "ALPACA_PAPER_API_SECRET",
    "ALPACA_LIVE_API_KEY",
    "ALPACA_LIVE_API_SECRET",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------- defaults


def test_default_settings_are_paper_only():
    settings = Settings(_env_file=None)
    assert settings.trading_env == "paper"
    assert settings.live_trading_allowed() is False


def test_default_broker_credentials_are_paper_pair():
    settings = Settings(_env_file=None, **PAPER_CREDS, **LIVE_CREDS)
    assert settings.broker_credentials() == ("paper-key", "paper-secret")


# ---------------------------------------------------------------- gate matrix


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"trading_env": "live"}, id="env-only"),
        pytest.param(
            {"trading_env": "live", "live_trading_enabled": True, **LIVE_CREDS},
            id="enabled-but-no-phrase",
        ),
        pytest.param(
            {
                "trading_env": "live",
                "live_trading_enabled": True,
                "live_trading_confirmation": LIVE_CONFIRMATION_PHRASE.lower(),
                **LIVE_CREDS,
            },
            id="wrong-phrase",
        ),
        pytest.param(
            {
                "trading_env": "live",
                "live_trading_enabled": True,
                "live_trading_confirmation": LIVE_CONFIRMATION_PHRASE,
                "alpaca_live_api_key": "",
                "alpaca_live_api_secret": "",
            },
            id="phrase-but-empty-creds",
        ),
        pytest.param(
            {**ALL_GATES, "alpaca_live_api_secret": ""},
            id="key-without-secret",
        ),
    ],
)
def test_any_single_missing_gate_blocks_live(overrides):
    settings = Settings(_env_file=None, **overrides)
    assert settings.live_trading_allowed() is False


def test_all_gates_satisfied_allows_live():
    settings = Settings(_env_file=None, **ALL_GATES, **PAPER_CREDS)
    assert settings.live_trading_allowed() is True
    assert settings.broker_credentials() == ("live-key", "live-secret")


def test_paper_env_stays_paper_even_with_live_gates_set():
    settings = Settings(_env_file=None, **{**ALL_GATES, "trading_env": "paper"})
    assert settings.live_trading_allowed() is False


# ---------------------------------------------------------------- credentials


def test_ungated_live_env_raises_instead_of_paper_fallback():
    settings = Settings(_env_file=None, trading_env="live", **PAPER_CREDS)
    with pytest.raises(RuntimeError, match="live-trading gates"):
        settings.broker_credentials()


def test_partially_gated_live_env_still_raises():
    settings = Settings(
        _env_file=None,
        trading_env="live",
        live_trading_enabled=True,
        **LIVE_CREDS,
        **PAPER_CREDS,
    )
    with pytest.raises(RuntimeError, match="refusing"):
        settings.broker_credentials()
