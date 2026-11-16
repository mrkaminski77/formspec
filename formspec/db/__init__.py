"""Database adapter registry and factory."""

from __future__ import annotations

from typing import Callable

from formspec.db.base import DatabaseAdapter
from formspec.db.sqlite import SQLiteAdapter

AdapterFactory = Callable[[str], DatabaseAdapter]

_ADAPTER_REGISTRY: dict[str, AdapterFactory] = {
    "sqlite": SQLiteAdapter,
}


def register_adapter(scheme: str, factory: AdapterFactory) -> None:
    """Register a DatabaseAdapter factory for a URI scheme (e.g., 'postgresql')."""
    _ADAPTER_REGISTRY[scheme.lower()] = factory


def get_adapter(dsn: str) -> DatabaseAdapter:
    """Instantiate a DatabaseAdapter from a DSN or filesystem path."""
    if "://" in dsn:
        scheme = dsn.split("://", 1)[0].lower()
    else:
        scheme = "sqlite"

    factory = _ADAPTER_REGISTRY.get(scheme)
    if factory is None:
        supported = ", ".join(sorted(_ADAPTER_REGISTRY))
        raise ValueError(
            f"Unsupported database scheme '{scheme}'. Supported schemes: {supported}."
        )
    return factory(dsn)


__all__ = ["DatabaseAdapter", "SQLiteAdapter", "get_adapter", "register_adapter"]
