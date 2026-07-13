"""External-oracle probes — adapters to an INDEPENDENT source of truth (the territory).

An :class:`~ooptdd.domain.ports.ExternalProbe` is a *port*: the engine asserts an ``external:``
gate check against it, never knowing what it reads. To corroborate a gate you write — or pick — an
*adapter* per independent source, exactly like a backend driver. The whole contract is ONE method::

    def probe(self, kind, selector, cid) -> ProbeResult(reachable, value, complete, separate_source)

So "make one as you need" is the intended model: a 5-line :class:`CallableProbe` for a quick
function, a reference :class:`~ooptdd.probes.file.FileProbe` /
:class:`~ooptdd.probes.http.HttpProbe`
for the common cases, or your own (a DB row, a second collector, a ledger). Resolution mirrors the
backend registry: built-ins, then the ``ooptdd.probes`` entry-point group (``pip install`` a driver,
no core change), then an instance passed in code.

CRUCIAL honesty: a probe only counts as *corroboration* (clears ``single_authority`` /
``require_corroboration``) when it declares ``separate_source=True`` — a genuinely different store /
service / filesystem than the one the system wrote its trace to. A probe re-reading the system's
own store is relocation, not independence, and must declare ``separate_source=False``.
"""
from __future__ import annotations

from ..domain.ports import ExternalProbe, ProbeResult

_BUILTINS = {
    "file": "ooptdd.probes.file:FileProbe",
    "http": "ooptdd.probes.http:HttpProbe",
}
_ENTRY_POINT_GROUP = "ooptdd.probes"


class CallableProbe:
    """The simplest adapter: wrap any ``fn(kind, selector, cid) -> value`` into an ExternalProbe.

    ``value is None`` means the fact is absent; a raised exception means the source was unreachable
    (``reachable=False`` → inconclusive, never a strict fail). Declare ``separate_source`` honestly:
    True only if ``fn`` reads a source genuinely independent of the system's trace store.
    """

    def __init__(self, fn, *, separate_source: bool = True):
        self._fn = fn
        self._separate = separate_source

    def probe(self, kind, selector, cid) -> ProbeResult:
        try:
            value = self._fn(kind, selector, cid)
        except Exception:  # noqa: BLE001 — an unreachable source is inconclusive, not a crash
            return ProbeResult(reachable=False, separate_source=self._separate)
        return ProbeResult(reachable=True, value=value, separate_source=self._separate)


def _load(target):
    if callable(target):
        return target
    mod_name, _, attr = target.partition(":")
    import importlib

    return getattr(importlib.import_module(mod_name), attr)


class ProbeRegistry:
    """Name → ExternalProbe driver registry: built-ins + the ``ooptdd.probes`` entry-point group,
    with in-code ``register``/``unregister`` — mirrors :class:`~ooptdd.backends.BackendRegistry`."""

    def __init__(self, builtins: dict | None = None, *,
                 entry_point_group: str = _ENTRY_POINT_GROUP):
        self._registered: dict[str, object] = dict(builtins if builtins is not None else _BUILTINS)
        self._entry_point_group = entry_point_group

    def register(self, name: str, target) -> None:
        self._registered[name] = target

    def unregister(self, name: str) -> None:
        self._registered.pop(name, None)

    def _entry_points(self) -> dict[str, object]:
        from importlib.metadata import entry_points

        return {ep.name: ep for ep in entry_points(group=self._entry_point_group)}

    def names(self) -> list[str]:
        return sorted(set(self._registered) | set(self._entry_points()))

    def resolve(self, name: str, **options) -> ExternalProbe:
        if name in self._registered:
            return _load(self._registered[name])(**options)
        ep = self._entry_points().get(name)
        if ep is not None:
            return ep.load()(**options)
        raise ValueError(
            f"unknown ooptdd probe {name!r}. built-ins: {sorted(self._registered)}; "
            f"or install a driver exposing the {self._entry_point_group!r} entry point."
        )


#: The process-wide registry the module-level helper delegates to.
default_registry = ProbeRegistry()


def get_probe(name: str, **options) -> ExternalProbe:
    """Return a configured probe instance ("file" | "http" | an ``ooptdd.probes`` entry point)."""
    return default_registry.resolve(name, **options)


__all__ = [
    "ExternalProbe", "ProbeResult", "CallableProbe",
    "ProbeRegistry", "default_registry", "get_probe",
]
