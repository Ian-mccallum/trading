"""Strategy registry: maps class paths in DB ``Strategy`` rows to Python
classes. Only classes explicitly registered here are loadable — the platform
never imports arbitrary class paths from the database, so a compromised DB row
cannot execute arbitrary code."""

from __future__ import annotations

from app.db.models import Strategy as StrategyRow
from app.strategies.base import Strategy

_REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    """Class decorator: @register on a Strategy subclass."""
    key = f"{cls.__module__}.{cls.__name__}"
    _REGISTRY[key] = cls
    _REGISTRY[cls.name] = cls  # short-name alias
    return cls


def get_strategy_class(class_path: str) -> type[Strategy]:
    try:
        return _REGISTRY[class_path]
    except KeyError:
        raise ValueError(
            f"strategy class {class_path!r} is not registered; "
            f"known: {sorted(k for k in _REGISTRY if '.' in k)}"
        ) from None


def instantiate(row: StrategyRow) -> Strategy:
    return get_strategy_class(row.class_path)(params=dict(row.params or {}))


def known_strategies() -> dict[str, type[Strategy]]:
    return {k: v for k, v in _REGISTRY.items() if "." in k}


def load_builtins() -> None:
    """Import builtin strategy modules so their @register decorators run."""
    import importlib
    import pkgutil

    import app.strategies.builtin as pkg
    from app.strategies import builtin  # noqa: F401

    for mod in pkgutil.iter_modules(pkg.__path__):
        importlib.import_module(f"app.strategies.builtin.{mod.name}")
