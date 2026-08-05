"""Machine vocabulary → plain language.

The single biggest thing separating a readable dashboard from a confusing one
is that the database speaks in identifiers (``order_notional``,
``c5_above_fast``, ``risk_verdict: rejected``) and a person needs sentences
with the numbers that actually caused the outcome.

Every mapping here is deliberately a *sentence fragment* that reads correctly
after a subject, not a noun phrase. "Rejected by " + phrase should scan.

Unknown keys degrade to a humanized version of the identifier rather than
raising or rendering a raw enum — new rules must never break the surface.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------- risk rules

#: rule name -> (short label, what it protects against)
RISK_RULES: dict[str, tuple[str, str]] = {
    "kill_switch": ("Kill switch", "An operator halted all trading."),
    "circuit_breaker": (
        "Circuit breaker",
        "Trading paused automatically after repeated broker errors.",
    ),
    "live_gate": (
        "Live-trading gate",
        "A live order was blocked because live trading is not fully enabled and armed.",
    ),
    "duplicate_order": (
        "Duplicate order",
        "A matching order was already sent moments ago.",
    ),
    "qty_sanity": ("Quantity check", "The order quantity was not a sane number."),
    "symbol_allowlist": (
        "Symbol allowlist",
        "This symbol is not on the list the platform may trade.",
    ),
    "stale_data": (
        "Stale market data",
        "The most recent price was too old to trade against.",
    ),
    "order_notional": ("Order size limit", "This single order was too large."),
    "position_limit": (
        "Position size limit",
        "The resulting position in this symbol would be too large.",
    ),
    "gross_exposure": (
        "Total exposure limit",
        "The combined value of all positions would be too large.",
    ),
    "daily_loss": ("Daily loss limit", "Losses today reached the configured limit."),
    "drawdown": ("Drawdown limit", "Equity fell too far from its peak."),
    "no_short_sell": (
        "Long-only rule",
        "The sell would have exceeded the shares actually held.",
    ),
    "broker_error": ("Broker error", "The broker rejected or failed the request."),
    "unknown_order": (
        "Unrecognized order",
        "An order exists at the broker with no local record.",
    ),
}

# ---------------------------------------------------------------- statuses

DECISION_STATUS: dict[str, tuple[str, str]] = {
    "proposed": ("Proposed", "neutral"),
    "approved": ("Approved", "ok"),
    "rejected": ("Rejected", "down"),
    "executed": ("Executed", "up"),
    "failed": ("Failed", "down"),
    "closed": ("Closed", "neutral"),
}

ORDER_STATUS: dict[str, tuple[str, str]] = {
    "pending_submit": ("Sending", "neutral"),
    "submitted": ("Submitted", "neutral"),
    "accepted": ("Working", "neutral"),
    "partially_filled": ("Partly filled", "ok"),
    "filled": ("Filled", "up"),
    "canceled": ("Canceled", "neutral"),
    "rejected": ("Rejected", "down"),
    "expired": ("Expired", "neutral"),
    "error": ("Error", "down"),
}

MODE_LABEL: dict[str, str] = {
    "paper": "Paper",
    "live": "Live",
    "shadow": "Shadow",
    "backtest": "Backtest",
}

REGIME_LABEL: dict[str, str] = {
    "trend_up": "Uptrend",
    "trend_down": "Downtrend",
    "high_vol_range": "Choppy, high volatility",
    "low_vol_range": "Rangebound, quiet",
    "unknown": "Not enough history",
}

# ---------------------------------------------------------------- strategy criteria

#: Minervini trend-template criteria, in the order they are published.
CRITERIA: dict[str, str] = {
    "c1_above_mid_and_slow": "Price above both the 150- and 200-day averages",
    "c2_mid_above_slow": "150-day average above the 200-day",
    "c3_slow_rising": "200-day average trending up for at least a month",
    "c4_fast_above_mid_and_slow": "50-day average above the 150- and 200-day",
    "c5_above_fast": "Price above the 50-day average",
    "c6_above_52w_low": "At least 25% above the 52-week low",
    "c7_near_52w_high": "Within 25% of the 52-week high",
    "c8_relative_strength": "Outperforming the benchmark",
}

#: Keys in a decision's context worth surfacing, with human labels.
CONTEXT_LABELS: dict[str, str] = {
    "close": "Price",
    "sma": "Moving average",
    "sma_period": "Average length",
    "trend_sma": "200-day average",
    "rsi": "RSI",
    "channel_high": "Breakout level",
    "channel_low": "Exit level",
    "exit_reason": "Exit trigger",
    "target_weight": "Target weight",
    "current_weight": "Current weight",
    "trailing_return": "12-month return",
    "relative_return": "Return vs benchmark",
    "ratio": "Share of 52-week high",
    "criteria_passed": "Criteria passed",
    "vol_scale": "Volatility scaling",
    "rebalance": "Rebalance action",
    "regime": "Market regime",
}

EXIT_REASONS: dict[str, str] = {
    "below_sma": "Price closed below the moving average",
    "channel_low": "Price broke the exit channel",
    "atr_stop": "Hit the volatility-based stop",
    "chandelier": "Hit the trailing stop",
    "stop": "Hit the stop loss",
    "target": "Reached the profit target",
    "template_fail": "No longer met the trend template",
    "below_sma_fast": "Closed below the 50-day average",
}


# ---------------------------------------------------------------- helpers


def humanize(identifier: str) -> str:
    """Fallback for anything not in a table: ``order_notional`` -> ``Order
    notional``. Keeps unknown values readable instead of leaking raw enums."""
    return identifier.replace("_", " ").strip().capitalize() or "Unknown"


def rule_label(rule: str) -> str:
    return RISK_RULES.get(rule, (humanize(rule), ""))[0]


def rule_explanation(rule: str) -> str:
    return RISK_RULES.get(rule, ("", ""))[1]


def status_label(status: str, table: dict[str, tuple[str, str]]) -> tuple[str, str]:
    """Returns (label, tone) where tone is up | down | ok | neutral."""
    return table.get(status, (humanize(status), "neutral"))


def criterion_label(key: str) -> str:
    return CRITERIA.get(key, humanize(key))


def context_label(key: str) -> str:
    return CONTEXT_LABELS.get(key, humanize(key))


def exit_reason_label(value: str) -> str:
    return EXIT_REASONS.get(value, humanize(value))


def rejection_sentence(rule: str, detail: dict[str, Any]) -> str:
    """A full sentence explaining one rejection, with its actual numbers.

    Falls back to the rule's stored reason, then to the generic explanation,
    so there is always something readable.
    """
    label = rule_label(rule)
    reason = str(detail.get("reason") or "").strip()
    if reason:
        # Reasons are engine-authored and already carry the numbers; just make
        # them a sentence under a human label.
        cleaned = reason[0].upper() + reason[1:] if reason else reason
        if not cleaned.endswith("."):
            cleaned += "."
        return f"{label}: {cleaned}"
    explanation = rule_explanation(rule)
    return f"{label}: {explanation}" if explanation else f"{label}."
