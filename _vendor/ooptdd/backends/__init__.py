"""Backend registry — resolve a driver name to a Backend instance.

Resolution order: built-ins, then the ``ooptdd.backends`` entry-point group
(so ``pip install ooptdd-loki`` adds a ``loki`` backend with no core change).

The registry is an explicit, injectable :class:`BackendRegistry` object so it can be built
and tested in isolation (register/unregister/names) without monkeypatching module globals
or installing entry points. The module-level :func:`get_backend` is a thin wrapper over a
process-wide :data:`default_registry`, so its signature and behavior are unchanged for the
CLI and plugin call sites.
"""
from __future__ import annotations

from .base import (
    DEFAULT_CAPS,
    Backend,
    BackendCaps,
    Clock,
    QueryResult,
    QuerySpec,
    SystemClock,
    TimeWindow,
    backend_caps,
    fetch,
)
from .memory import MemoryBackend
from .memory import reset as memory_reset

_BUILTINS = {
    "memory": "ooptdd.backends.memory:MemoryBackend",
    "jsonl": "ooptdd.backends.jsonl:JsonlBackend",  # 영속·cross-process·zero-infra queryable
    "openobserve": "ooptdd.backends.openobserve:OpenObserveBackend",
    "otel": "ooptdd.backends.otel:OtelBackend",
    "clickhouse": "ooptdd.backends.clickhouse:ClickHouseBackend",
    "signoz": "ooptdd.backends.clickhouse:ClickHouseBackend",  # SigNoz = ClickHouse tables
    "victorialogs": "ooptdd.backends.victorialogs:VictoriaLogsBackend",
}

_ENTRY_POINT_GROUP = "ooptdd.backends"


def _load(target):
    """Resolve a driver target to a class/factory. A ``"module:attr"`` string is imported
    lazily (so importing this package never imports every driver); a callable passes through
    (an in-code registration)."""
    if callable(target):
        return target
    mod_name, _, attr = target.partition(":")
    import importlib

    return getattr(importlib.import_module(mod_name), attr)


class BackendRegistry:
    """An explicit name → driver registry. Built-ins plus the ``ooptdd.backends`` entry-point
    group, with in-code ``register``/``unregister`` and ``resolve`` — testable without globals."""

    def __init__(self, builtins: dict | None = None, *,
                 entry_point_group: str = _ENTRY_POINT_GROUP):
        self._registered: dict[str, object] = dict(
            builtins if builtins is not None else _BUILTINS)
        self._entry_point_group = entry_point_group

    def register(self, name: str, target) -> None:
        """Register a driver under ``name`` (a ``"module:attr"`` string or a class/factory).
        Overrides any prior registration of that name (loud-by-test, not silent in practice)."""
        self._registered[name] = target

    def unregister(self, name: str) -> None:
        self._registered.pop(name, None)

    def _entry_points(self) -> dict[str, object]:
        from importlib.metadata import entry_points

        return {ep.name: ep for ep in entry_points(group=self._entry_point_group)}

    def names(self) -> list[str]:
        """Every resolvable name: registered/built-in first, then discovered entry points."""
        return sorted(set(self._registered) | set(self._entry_points()))

    def resolve(self, name: str, **options) -> Backend:
        """Instantiate the backend named ``name`` with ``options``. Registered names win over
        entry points; an unknown name raises ``ValueError`` listing what is available."""
        if name in self._registered:
            return _load(self._registered[name])(**options)
        ep = self._entry_points().get(name)
        if ep is not None:
            return ep.load()(**options)
        raise ValueError(
            f"unknown ooptdd backend {name!r}. built-ins: {sorted(self._registered)}; "
            f"or install a driver exposing the {self._entry_point_group!r} entry point."
        )


#: The process-wide registry the module-level helpers delegate to.
default_registry = BackendRegistry()


def get_backend(name: str, **options) -> Backend:
    """Return a configured backend instance.

    ``name`` is a built-in ("memory" | "openobserve" | "otel" | "clickhouse" | "signoz" |
    "victorialogs") or an entry point registered under ``ooptdd.backends``. ``options`` are
    passed to the driver. Thin wrapper over :data:`default_registry`."""
    return default_registry.resolve(name, **options)


__all__ = [
    "Backend",
    "QueryResult",
    "MemoryBackend",
    "get_backend",
    "memory_reset",
    "BackendRegistry",
    "default_registry",
    "BackendCaps",
    "DEFAULT_CAPS",
    "QuerySpec",
    "TimeWindow",
    "Clock",
    "SystemClock",
    "backend_caps",
    "fetch",
]
