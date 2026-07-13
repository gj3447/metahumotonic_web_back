"""Domain ports — the abstractions the engine depends on, owned by the domain layer.

The :class:`Backend` Protocol is a *port* (hexagonal architecture / dependency inversion):
the engine (gate, verify) is written against this interface, and concrete drivers
(memory, OpenObserve, OTLP, …) are *adapters* that implement it in :mod:`ooptdd.backends`.
Keeping the port here — not in the adapter package — is what lets the dependency arrow
point engine → domain (never engine → a concrete adapter), enforced by the architecture
fitness test.

Beyond the backend, this module owns the small **value objects and ports the engine reads
against**: :class:`QueryResult` (an answer, with its completeness/reachability honesty),
:class:`TimeWindow` / :class:`QuerySpec` (a typed *query intent* instead of bare kwargs),
:class:`BackendCaps` (typed capabilities instead of ad-hoc ``getattr``), and the
:class:`Clock` port (injectable time, so the engine's polling is deterministic and
sleep-free under test). The bridge functions :func:`backend_caps` and :func:`fetch` let the
engine use the typed surface while every legacy two-method backend keeps working untouched.

A backend does exactly two required things: ``ship(events)`` (write) and
``query(cid, *, since_us, until_us)`` (read back, reporting whether the query itself was
*reachable* and *complete*). The interesting logic — the LTL₃ verdict, the polling window,
the clock-skew buffer — lives *above* the port, identical no matter where events land.

Two load-bearing honesty fields on :class:`QueryResult`:
  - ``reachable`` distinguishes "the store says no such event" (absent / ⊥) from "I could
    not even ask the store" (inconclusive / ?). Conflating them turns a blip into a flake.
  - ``complete`` distinguishes a full answer from a partial one (a paging/row cap was hit).
    A truncated read may undercount or hide an offender, so the verdict layer must refuse to
    treat ``complete=False`` as a clean pass — the same discipline as ``reachable``.
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class QueryResult:
    """Outcome of a single backend query.

    reachable: True iff the query round-trip succeeded (regardless of hits). False means
               the store was unreachable -> verdict stays `?` (inconclusive).
    events:    the matching event envelopes (dicts), newest-or-any order.
    complete:  True iff the backend returned *every* matching row for the window. False iff a
               paging/row cap was hit and the set is partial — incomplete evidence, which the
               verdict layer must not treat as a clean pass. A full read leaves this True.
    error:     None on a clean round-trip; else a short "TypeError: msg" attribution of WHY the
               query failed (a 401 vs a DNS failure vs an unconfigured store), so a reachable=False
               is diagnosable instead of an anonymous outage. Never gates the verdict — advisory.
    """

    reachable: bool
    events: list[dict] = field(default_factory=list)
    complete: bool = True
    error: str | None = None


# ── time: an injectable Clock port + a typed query window ───────────────────────

class Clock(Protocol):
    """The time port. The engine reads ``now_us()`` instead of calling ``time.time()`` so
    polling windows and retry loops are deterministic (and sleep-free) under a fake clock."""

    def now_us(self) -> int:
        """Current wall-clock time in epoch microseconds."""
        ...


class SystemClock:
    """The real clock — epoch microseconds from ``time.time()``. The default everywhere."""

    def now_us(self) -> int:
        return int(time.time() * 1_000_000)


@dataclass(frozen=True)
class TimeWindow:
    """A microsecond readback window ``[since_us, until_us]`` (store-receive time)."""

    since_us: int
    until_us: int

    @classmethod
    def around_now(cls, clock: Clock, lookback_s: int, future_buffer_s: int) -> TimeWindow:
        """The window a poll uses: ``[now - lookback, now + future_buffer]``. The future
        buffer absorbs receive-time / clock-skew (a record stamped just after we start
        looking). Integer arithmetic identical to the historical inline computation."""
        now_us = clock.now_us()
        return cls(now_us - lookback_s * 1_000_000, now_us + future_buffer_s * 1_000_000)


@dataclass(frozen=True)
class QuerySpec:
    """A typed query *intent* handed to a backend, instead of loose kwargs: which cid, over
    what window, with an optional row ``limit`` / paging ``cursor`` / ``where`` filter. A
    backend that implements ``query_spec`` reads it directly; legacy backends are driven via
    :func:`fetch`, which translates it to the two-kwarg ``query`` call.

    ``cid`` + ``window`` are the **live read path**: the engine builds ``QuerySpec(cid, window)``
    (see ``engine/gate.py`` + ``engine/verify.py``) and every shipped backend honours them.
    ``limit`` / ``cursor`` / ``where`` are **reserved, forward-compat** fields — no shipped
    backend implements ``query_spec`` yet, so :func:`fetch` *drops* them on the legacy ``query``
    path. The engine must therefore never rely on server-side paging/filter a legacy driver
    can't honour; ``where`` in particular is filtered in Python by design (dialect-neutral and
    injection-safe — see ``BackendCaps.supports_where``). Pushing any of the three down is a
    per-driver ``query_spec`` opt-in, not a default. The contract is pinned by
    ``test_fetch_drops_reserved_queryspec_fields_for_legacy_backends``."""

    cid: str
    window: TimeWindow
    limit: int | None = None  # reserved: server-side row cap (query_spec opt-in only)
    cursor: str | None = None  # reserved: server-side paging cursor (query_spec opt-in only)
    where: dict | None = None  # reserved: server-side filter; default path filters in Python


# ── capabilities: typed, not ad-hoc getattr ─────────────────────────────────────

@dataclass(frozen=True)
class BackendCaps:
    """What a backend can do, as data instead of scattered ``getattr`` probes.

    queryable:     has a read side (False = write-only, e.g. OTLP; strict verify impossible).
    paginates:     reads to completion across pages (so ``complete`` is meaningful).
    supports_where: can filter server-side (informational; ooptdd filters in Python anyway).
    write_only:    convenience inverse of ``queryable`` for call sites that read positively.
    """

    queryable: bool = True
    paginates: bool = False
    supports_where: bool = False
    write_only: bool = False


DEFAULT_CAPS = BackendCaps()


@runtime_checkable
class Backend(Protocol):
    """Minimal driver contract. Implement these two methods; that's a backend.

    A driver MAY additionally expose ``caps: BackendCaps`` and/or ``query_spec(spec)`` for the
    typed surface — both optional and read via :func:`backend_caps` / :func:`fetch`, so a
    plain two-method backend (and every test fake) still structurally satisfies this Protocol.
    """

    #: Per-backend polling hints (seconds). Stores with slow ingest override these.
    default_lookback_s: int
    default_future_buffer_s: int
    #: False iff the backend has no read side (e.g. OTLP/otel is write-only). The verify
    #: layer cannot confirm arrival on such a backend, so `strict` over it is meaningless —
    #: callers surface that loudly rather than passing silently. Read via :func:`backend_caps`.
    queryable: bool = True

    def ship(self, events: list[dict]) -> None:
        """Write events. Must be best-effort; raising is allowed but the caller
        treats a ship failure as a warning, never a build failure."""
        ...

    def query(self, cid: str, *, since_us: int, until_us: int) -> QueryResult:
        """Return events carrying ``cid`` whose store timestamp falls in the
        microsecond window ``[since_us, until_us]``."""
        ...


def backend_caps(backend) -> BackendCaps:
    """The single place capability logic lives: a backend's ``caps`` if it has one, else
    synthesized from the legacy ``queryable`` attribute. Bridges old and new drivers so the
    engine never hand-rolls ``getattr(backend, 'queryable', True)`` again."""
    caps = getattr(backend, "caps", None)
    if isinstance(caps, BackendCaps):
        return caps
    queryable = getattr(backend, "queryable", True)
    return BackendCaps(queryable=queryable, write_only=not queryable)


def backend_identity(backend) -> str:
    """A best-effort, framework-DERIVED identity for WHERE a backend reads/writes — the basis for
    emit provenance (``oracle.emit_identity``) and for demoting an ``external:`` probe that re-reads
    this very endpoint (its ``separate_source`` claim then cannot be honest). Prefers a
    driver's own ``identity()``; else the resolved endpoint URL — read *now* from the env var the
    driver was configured with (``url_env``), i.e. the real target, not a name the backend chose to
    report; else the class name (an in-process backend like memory has no endpoint to compare, so
    relocation *through* it is out of scope). This is NOT a security boundary — a backend or probe
    can still misreport — it only makes the common, honest cases comparable so a provably-same
    endpoint can be caught. The single-authority residue (shared data lineage, a colluding source)
    is named in METHODOLOGY.md and cannot be discharged here, only surfaced."""
    ident = getattr(backend, "identity", None)
    if callable(ident):
        try:
            got = ident()
        except Exception:  # noqa: BLE001 — identity is best-effort; it must never break a verdict
            got = None
        if got:
            return str(got)
    url_env = getattr(backend, "url_env", None)
    if url_env:
        url = os.getenv(url_env)
        if url:
            return url.rstrip("/")
    return type(backend).__name__


def fetch(backend, spec: QuerySpec, clock: Clock | None = None) -> QueryResult:
    """Read a backend through one typed entry point regardless of its generation: use
    ``query_spec(spec)`` if the driver implements it, else translate the :class:`QuerySpec`
    into the legacy ``query(cid, since_us=, until_us=)`` call. This shim is what lets every
    existing backend (and test fake) keep working while the engine speaks ``QuerySpec``."""
    query_spec = getattr(backend, "query_spec", None)
    if callable(query_spec):
        return query_spec(spec)
    return backend.query(spec.cid, since_us=spec.window.since_us, until_us=spec.window.until_us)


# ── the independent-oracle port (breaks self-consistency) ───────────────────────
# Every other verdict input is the system's own emit, read back from a store the system writes —
# so a green proves self-CONSISTENCY, not correctness. An ExternalProbe is the one input that does
# NOT come from the trace: it reads a fact from the TERRITORY (a DB row, a file, a second
# collector). An `external:` gate check asserts against it, so a green there means more than the
# system agreeing with itself. It mirrors QueryResult's honesty fields so the engine treats a
# missing probe as a loud misconfiguration (never a silent green) and an unreachable probe as
# inconclusive (never a strict fail) — extending, not bypassing, the reachable/complete lattice.

@dataclass
class ProbeResult:
    """Outcome of one external-state probe.

    reachable: True iff the probe round-trip succeeded (regardless of value). False -> `?`.
    value:     the external fact (any value), or None if absent / not extracted.
    complete:  True iff the probe read the full fact (no truncation).
    """

    reachable: bool
    value: object = None
    complete: bool = True
    #: The probe author's DECLARATION that the fact comes from a genuinely separate source of
    #: truth (a different store/service/filesystem), not the same store the system wrote — only a
    #: separate_source=True probe counts as independent CORROBORATION (closes the relocation hole:
    #: a probe re-reading the system's own store is self-consistency moved one layer out). ooptdd
    #: trusts this declaration; it cannot itself prove a source is independent.
    separate_source: bool = False
    #: A framework-COMPARABLE identity for WHERE this fact was actually read (a path / URL / DSN).
    #: Unlike ``separate_source`` (a bare claim), this is a value the engine can check: if it equals
    #: the emit backend's identity, the probe demonstrably re-read the system's own endpoint, so the
    #: ``separate_source`` claim is provably false and is DEMOTED (relocation, not independence).
    #: The demotion is ASYMMETRIC — a derived identity can only FALSIFY a declared True, never
    #: promote a missing one — so a genuinely-separate source whose identity the probe cannot
    #: report (``None``) keeps its declared bool. Reference probes (file/http) report it.
    derived_identity: str | None = None


@runtime_checkable
class ExternalProbe(Protocol):
    """The independent-oracle port. Optional everywhere (default None): the engine treats a
    missing probe as ``no_external_probe_configured`` (loud, never a silent green) and an
    unreachable probe as inconclusive."""

    def probe(self, kind: str, selector: object, cid: str) -> ProbeResult:
        """Resolve ``(kind, selector)`` for ``cid`` to a :class:`ProbeResult`. ``kind`` names the
        fact family the probe understands (``db_row`` / ``file`` / ``http`` / …); ``selector`` is
        the probe-specific locator — NOT the system's own emitted event."""
        ...


#: A sleeper is the injectable counterpart to the Clock for the retry loop's waits.
Sleeper = Callable[[float], None]
