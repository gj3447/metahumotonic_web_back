"""Backend conformance kit — one shared contract every Backend driver must satisfy.

``pip install``-ing a third-party driver (registered under the ``ooptdd.backends`` entry
point) is only safe if the driver actually honours the port's contract: ship-then-query
round-trips, whole rows come back (so gate ``where:`` filters see real fields), each event
carries a ``_timestamp``, the correlation id is bound safely (a cid with quotes/specials
round-trips and never breaks or injects), and a normal read reports ``complete``. This module
gives driver authors a single function to assert all of that against a store they can write
to — and ooptdd self-tests it against the reference :class:`~ooptdd.backends.memory.MemoryBackend`.

Usage (in a driver's own test suite)::

    from ooptdd.backends.conformance import assert_backend_conforms
    def test_my_driver_conforms():
        assert_backend_conforms(lambda: MyBackend(...))   # raises AssertionError on any gap

It lives in the adapter layer (it drives a concrete backend and runs the engine's gate over
the result), so it never widens the engine's dependency surface.
"""
from __future__ import annotations

from collections.abc import Callable

from ..domain.ports import backend_caps
from ..engine.gate import evaluate

#: A factory returning a *fresh, write-and-read* backend bound to whatever store the author
#: wants exercised (the in-memory default, or a real OpenObserve/ClickHouse/… via env/opener).
BackendFactory = Callable[[], object]


def assert_backend_conforms(make_backend: BackendFactory, *, cid: str = "ooptdd-conf-cid") -> None:
    """Assert the backend ``make_backend()`` honours the Backend contract. Raises
    ``AssertionError`` on the first violation, naming what failed.

    Covers: ship→query round-trip, whole-row (``where:``) passthrough, ``_timestamp``
    passthrough, injection-safe cid binding, and the ``complete`` completeness flag. The
    backend must actually persist and read back (the memory backend, or a real store); a
    write-only driver (``queryable=False``) is out of scope for read conformance.
    """
    backend = make_backend()
    # 1. ship → query round-trip: every shipped event for the cid comes back.
    events = [
        {"cid": cid, "event": "alpha", "verdict": "PASS", "n": 1},
        {"cid": cid, "event": "alpha", "verdict": "NG", "n": 2},
        {"cid": cid, "event": "beta", "verdict": "PASS", "n": 3},
    ]
    backend.ship(events)
    res = backend.query(cid, since_us=0, until_us=10**19)
    assert res.reachable, "query of a just-shipped cid must be reachable"
    got = res.events
    assert len(got) >= 3, f"round-trip lost events: shipped 3, read {len(got)}"

    # 2. whole-row passthrough: arbitrary fields survive so gate `where:` can filter on them.
    alphas_ng = [e for e in got if e.get("event") == "alpha" and e.get("verdict") == "NG"]
    assert len(alphas_ng) == 1, "whole-row passthrough lost the `verdict` field"

    # 3. _timestamp passthrough: ordering checks need a store-receive timestamp.
    assert all("_timestamp" in e for e in got), "every returned event must carry _timestamp"

    # 4. completeness: a normal, in-window read reports complete (a full answer).
    assert getattr(res, "complete", True) is True, "a complete read must report complete=True"

    # 5. the gate runs over the rows like any backend (the whole portability point).
    gate = evaluate(backend, {"cid": cid, "expect": [
        {"event": "alpha", "where": {"verdict": "NG"}, "op": "==", "count": 1},
        {"present": [{"event": "beta"}]},
    ]})
    assert gate["ok"], f"gate over conformant rows should pass, got {gate}"

    # 6. injection-safe cid binding: a cid with quotes/specials round-trips, never breaks.
    nasty = "conf'\"; OR 1=1 --:cid"
    b2 = make_backend()
    b2.ship([{"cid": nasty, "event": "safe", "marker": "yes"}])
    r2 = b2.query(nasty, since_us=0, until_us=10**19)
    assert r2.reachable, "a cid with special characters must still query cleanly"
    assert any(e.get("marker") == "yes" for e in r2.events), \
        "a cid with quotes/specials must round-trip (cid binding must be escaped/parameterized)"


#: A factory for write-only conformance: returns ``(backend, capture)`` where ``backend`` is a
#: fresh write-only driver (``queryable=False``, e.g. OTLP) wired to ``capture`` — any object
#: exposing a ``records: list[dict]`` of what the driver actually shipped (e.g. an OTLP
#: ``InMemoryLogExporter`` adapter). The sink stands in for the store-side reader the driver lacks.
WriteOnlyHarness = Callable[[], tuple]


def assert_writeonly_backend_conforms(
    make: WriteOnlyHarness, *, cid: str = "ooptdd-wo-cid"
) -> None:
    """Assert a *write-only* driver (``queryable=False``, e.g. OTLP) honours the contract it
    *can* — there is no read side, so the round-trip :func:`assert_backend_conforms` cannot apply.
    Instead the driver is paired with a capture sink (``capture.records``) that records what it
    shipped, and we assert **export fidelity** against it, plus that the driver is *honestly*
    write-only: its ``query`` returns ``reachable=False`` (inconclusive ?), never a silent
    ``absent`` (⊥) — so ``strict`` verification over it is loudly impossible, not a false green.

    Usage (in a write-only driver's own test suite)::

        def test_my_otlp_driver_conforms():
            def harness():
                cap = InMemoryCapture()              # adapts an OTLP InMemoryLogExporter
                return MyOTLPBackend(exporter=cap), cap
            assert_writeonly_backend_conforms(harness)
    """
    backend, capture = make()
    events = [
        {"cid": cid, "event": "alpha", "verdict": "PASS", "n": 1},
        {"cid": cid, "event": "beta", "verdict": "NG", "n": 2},
    ]
    backend.ship(events)

    # 1. export fidelity: the capture sink received every shipped event.
    recs = list(getattr(capture, "records", []))
    assert len(recs) >= len(events), \
        f"write-only export dropped events: shipped {len(events)}, captured {len(recs)}"

    # 2. payload fidelity: arbitrary fields survive the wire format (so a reader can filter).
    assert any(r.get("event") == "beta" and r.get("verdict") == "NG" for r in recs), \
        "write-only export lost an event field (whole-row fidelity)"

    # 3. honestly write-only: caps must say so (callers that read positively skip it loudly).
    caps = backend_caps(backend)
    assert caps.write_only is True, \
        "a write-only conformance target must report caps.write_only=True (queryable=False)"

    # 4. the read side is honest: query is inconclusive (reachable=False), never a silent absent.
    # A write-only driver that returns reachable=True would let `strict` read a false ⊥ off it.
    res = backend.query(cid, since_us=0, until_us=10**19)
    assert res.reachable is False, \
        "a write-only backend's query must be inconclusive (reachable=False), never a silent absent"
