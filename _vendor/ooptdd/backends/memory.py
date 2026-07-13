"""In-process backend — zero network, zero infra, deterministic.

This is the default backend and the single biggest lever for adoption: you can
run the whole ooptdd loop (ship -> verify -> verdict) in a plain `pytest` with
nothing installed and nothing running. The demo, the plugin's own test suite,
and most users' first green all ride on this.

Events live in a module-global store so that a `ship` in one place and a
`verify` in another (e.g. the pytest session hook) see the same data within a
process. `reachable` is always True — there is no network to fail — so the only
verdicts you can get are `present` or `absent`, never `inconclusive`. That makes
it perfect for reproducing the silent-ingest-loss bug on purpose.
"""
from __future__ import annotations

import itertools
import time

from .base import BackendCaps, QueryResult

# process-global monotonic sequence, stamped per shipped event. Survives reset() so ordering stays
# globally monotonic within a process — this is what breaks a same-batch wall-clock tie (every event
# in one ship() shares now_us, but each gets a distinct, increasing _seq).
_SEQ = itertools.count()
# process-global store: cid -> list[(stored_us, seq, event)]
_STORE: dict[str, list[tuple[int, int, dict]]] = {}


def reset() -> None:
    """Clear the store (handy between tests)."""
    _STORE.clear()


class MemoryBackend:
    """A fake store that keeps events in a dict. Drop-in for CI and demos."""

    default_lookback_s = 3600
    default_future_buffer_s = 0
    queryable = True  # in-process store reads back deterministically
    # The reference backend: reads everything in one shot (always complete) and filters in
    # Python, so the conformance kit validates the typed-caps contract against it.
    caps = BackendCaps(queryable=True, paginates=False, supports_where=True)

    def __init__(self, *, drop: bool = False, **_ignored):
        # ``drop=True`` silently discards everything shipped — this is how the
        # killer demo simulates a backend that accepts then loses your events.
        self.drop = drop

    def ship(self, events: list[dict]) -> None:
        if self.drop or not events:
            return
        now_us = int(time.time() * 1_000_000)
        for ev in events:
            cid = ev.get("cid") or ev.get("correlation_id") or ev.get("cycle_id") or ""
            _STORE.setdefault(cid, []).append((now_us, next(_SEQ), ev))

    def query(self, cid: str, *, since_us: int, until_us: int) -> QueryResult:
        # Stamp each returned event with its store-receive time under ``_timestamp``
        # (µs), mirroring OpenObserve's native column, so ordering checks
        # (gate ``must_order``) work uniformly across backends. Copy, don't mutate.
        hits = [
            {**ev, "_timestamp": ts, "_seq": seq}
            for (ts, seq, ev) in _STORE.get(cid, [])
            if since_us <= ts <= until_us
        ]
        return QueryResult(reachable=True, events=hits)
