"""Owner profile: the "customizable to me" layer.

One file describes how *this* operator wants the platform to behave — how much
capital it may work with, how aggressively to size, which symbols and personas
matter, what to never touch.

**The profile can only ever be more conservative than the risk engine.** It
feeds *intent construction* (which symbols, what quantity), never risk
evaluation. Every intent it produces still passes the same `RiskEngine`, so a
mistake here cannot loosen a limit — at worst it proposes something the engine
then rejects.

Format is TOML, read with the stdlib ``tomllib`` (Python 3.11+). The spec
sketched YAML, but that would mean a new dependency for a single config file;
TOML costs nothing, supports comments, and is stricter about types.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.logging import get_logger

log = get_logger("profile.model")

DEFAULT_PATH = Path("profile.toml")

#: Risk appetite scales the per-position target. Deliberately narrow: this is a
#: preference dial, not a leverage control, and the widest setting still cannot
#: exceed what the risk engine permits.
RISK_MULTIPLIERS: dict[str, Decimal] = {
    "conservative": Decimal("0.5"),
    "moderate": Decimal("1.0"),
    "aggressive": Decimal("1.5"),
}


@dataclass(frozen=True)
class PersonaWeight:
    name: str
    weight: Decimal


@dataclass(frozen=True)
class Profile:
    """Owner preferences. Immutable once loaded."""

    name: str = "default"
    #: Notional the bot may work with. NOT the account size — deliberately
    #: separate so the operator can hand the bot a slice of a larger account.
    capital_allocation: Decimal = Decimal(25_000)
    risk_appetite: str = "moderate"
    max_positions: int = 5
    symbols: tuple[str, ...] = ()
    exclusions: frozenset[str] = frozenset()
    benchmark: str = "SPY"
    personas: tuple[PersonaWeight, ...] = ()
    #: Hard ceiling on any single position as a fraction of capital_allocation.
    max_position_fraction: Decimal = Decimal("0.35")
    #: Orders below this notional are not worth the spread.
    min_order_notional: Decimal = Decimal(100)

    @property
    def risk_multiplier(self) -> Decimal:
        return RISK_MULTIPLIERS[self.risk_appetite]

    def persona_weight(self, name: str) -> Decimal:
        """Weight for a persona, defaulting to 1 when unlisted.

        Unlisted personas are not silently disabled: activation is an admin
        decision, and a profile that forgot to mention one should not quietly
        stop it trading.
        """
        for entry in self.personas:
            if entry.name == name:
                return entry.weight
        return Decimal(1)

    def allows_symbol(self, symbol: str) -> bool:
        upper = symbol.upper()
        if upper in self.exclusions:
            return False
        return not self.symbols or upper in self.symbols

    def per_position_target(self) -> Decimal:
        """Base notional for one position, before persona weighting."""
        base = self.capital_allocation / Decimal(self.max_positions)
        return base * self.risk_multiplier


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"profile field {field_name!r} must be a number, got {value!r}")
    try:
        return Decimal(str(value))
    except Exception:
        raise ValueError(f"profile field {field_name!r} is not a valid number: {value!r}")


def parse_profile(data: dict[str, Any]) -> Profile:
    """Build a Profile from raw config, validating every field.

    Validation is strict and total: a typo in this file changes what the bot
    trades, so it must fail loudly at load rather than silently default.
    """
    name = str(data.get("name", "default"))

    capital = _decimal(data.get("capital_allocation", 25_000), "capital_allocation")
    if capital <= 0:
        raise ValueError(f"capital_allocation must be > 0, got {capital}")

    appetite = str(data.get("risk_appetite", "moderate")).lower()
    if appetite not in RISK_MULTIPLIERS:
        raise ValueError(
            f"risk_appetite must be one of {sorted(RISK_MULTIPLIERS)}, got {appetite!r}"
        )

    max_positions = data.get("max_positions", 5)
    if isinstance(max_positions, bool) or not isinstance(max_positions, int) or max_positions < 1:
        raise ValueError(f"max_positions must be an integer >= 1, got {max_positions!r}")

    def _symbol_list(key: str) -> list[str]:
        raw = data.get(key, [])
        if not isinstance(raw, list) or any(not isinstance(s, str) or not s.strip() for s in raw):
            raise ValueError(f"profile field {key!r} must be a list of symbol strings")
        return [s.strip().upper() for s in raw]

    symbols = tuple(_symbol_list("symbols"))
    exclusions = frozenset(_symbol_list("exclusions"))
    overlap = set(symbols) & exclusions
    if overlap:
        raise ValueError(
            f"symbols and exclusions overlap: {sorted(overlap)}. "
            "A symbol cannot be both tradeable and excluded."
        )

    personas: list[PersonaWeight] = []
    raw_personas = data.get("personas", [])
    if not isinstance(raw_personas, list):
        raise ValueError("profile field 'personas' must be a list of tables")
    for entry in raw_personas:
        if not isinstance(entry, dict) or "name" not in entry:
            raise ValueError(f"each persona entry needs a 'name', got {entry!r}")
        weight = _decimal(entry.get("weight", 1), "persona weight")
        if not 0 < weight <= 2:
            raise ValueError(
                f"persona weight for {entry['name']!r} must be in (0, 2], got {weight}"
            )
        personas.append(PersonaWeight(name=str(entry["name"]), weight=weight))

    max_fraction = _decimal(data.get("max_position_fraction", "0.35"), "max_position_fraction")
    if not 0 < max_fraction <= 1:
        raise ValueError(f"max_position_fraction must be in (0, 1], got {max_fraction}")

    min_notional = _decimal(data.get("min_order_notional", 100), "min_order_notional")
    if min_notional < 0:
        raise ValueError(f"min_order_notional must be >= 0, got {min_notional}")

    benchmark = str(data.get("benchmark", "SPY")).strip().upper()
    if not benchmark:
        raise ValueError("benchmark must be a non-empty symbol")

    return Profile(
        name=name,
        capital_allocation=capital,
        risk_appetite=appetite,
        max_positions=max_positions,
        symbols=symbols,
        exclusions=exclusions,
        benchmark=benchmark,
        personas=tuple(personas),
        max_position_fraction=max_fraction,
        min_order_notional=min_notional,
    )


def load_profile(path: Path | str | None = None) -> Profile:
    """Load the profile, or return defaults when no file exists.

    A missing profile is normal — the platform runs perfectly well on
    defaults. A *malformed* profile is not, and raises.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    if not target.exists():
        log.info("profile_default", reason="no profile file", path=str(target))
        return Profile()
    with target.open("rb") as handle:
        data = tomllib.load(handle)
    profile = parse_profile(data)
    log.info(
        "profile_loaded",
        path=str(target),
        name=profile.name,
        capital=str(profile.capital_allocation),
        appetite=profile.risk_appetite,
        symbols=len(profile.symbols),
    )
    return profile
