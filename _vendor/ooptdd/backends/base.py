"""Backend abstraction — the portability seam (adapter side).

The driver *contract* — the :class:`Backend` Protocol and :class:`QueryResult` — is a
domain **port**, defined in :mod:`ooptdd.domain.ports` so the engine can depend on it
without depending on any concrete adapter. This module re-exports it for the adapter
layer: concrete drivers (memory, openobserve, otel, …) import ``Backend``/``QueryResult``
from here, and third-party drivers register under the ``ooptdd.backends`` entry-point group.

Drivers are discovered three ways, in order:
  1. built-ins (memory, openobserve, otel)
  2. the ``ooptdd.backends`` entry-point group (``pip install`` a 3rd-party driver)
  3. an explicit instance passed in code
"""
from __future__ import annotations

from ..domain.ports import (
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

__all__ = [
    "Backend",
    "QueryResult",
    "BackendCaps",
    "DEFAULT_CAPS",
    "QuerySpec",
    "TimeWindow",
    "Clock",
    "SystemClock",
    "backend_caps",
    "fetch",
    "raise_for_status",
]


def raise_for_status(response) -> None:
    """Raise if an HTTP response carries a non-2xx status, so a dropped ingest/read surfaces
    as a *loud* failure (the caller downgrades a ship failure to a warning; a query failure
    becomes ``reachable=False``) instead of being silently treated as success. Tolerates a
    response object with no status (e.g. a test's mock opener) by treating it as success —
    real ``urllib`` responses always carry one and also raise on 4xx/5xx themselves."""
    status = getattr(response, "status", None)
    if status is None and hasattr(response, "getcode"):
        try:
            status = response.getcode()
        except Exception:
            status = None
    if status is not None and status >= 400:
        raise OSError(f"backend returned HTTP {status}")


# module-private alias used by the driver modules
_raise_for_status = raise_for_status
